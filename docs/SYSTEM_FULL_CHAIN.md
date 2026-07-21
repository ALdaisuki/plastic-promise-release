# Plastic Promise — System Chain Overview

> Release-facing overview. This document describes the system shape and operating principles without exposing private planning artifacts.
>
> 版本: 0.1.19 | 日期: 2026-07-21

## 1. What this system is

Plastic Promise is a local-first coordination runtime for AI-assisted software work. It connects memory, context retrieval, principles, audit, trust, task dispatch, and feedback loops into one operating chain.

The goal is not to store everything forever. The goal is to keep useful commitments alive, let stale context decay, and make each action traceable enough that future agents can continue work safely.

## 2. Core chain

```text
session start
  -> retrieve context and principles
  -> check trust and audit boundaries
  -> execute a small reversible step
  -> verify the result
  -> close the loop
  -> store better future context
```

Every serious task should pass through this loop:

1. **Start with context** — load the current task, relevant memory, and active principles.
2. **Act inside boundaries** — apply trust, audit, and git governance before changing shared state.
3. **Keep work traceable** — prefer small branches, small commits, and reviewable diffs.
4. **Verify before claiming done** — tests, compile checks, or manual verification should match the change type.
5. **Close the loop** — record what changed, what was learned, and what should influence future work.

## 3. Main subsystems

| Subsystem | Role |
|---|---|
| MCP Server | Exposes the runtime to Claude Code and other MCP clients over stdio or SSE. |
| Dashboard V2 | Provides a Chinese, loopback-only, project-scoped and read-only operator surface at `/dashboard`. |
| Launcher runtime modes | Select startup depth and Rust acceleration before services start, with MCP hot updates through `runtime_mode`. |
| Service bootstrap | Launcher child processes inherit runtime-mode env and receive project root on `PYTHONPATH`; MCP health binds PID/source root/revision before startup or reuse is accepted. |
| Memory | Stores reusable experience, decisions, preferences, task knowledge, and derived signals. |
| Context | Retrieves the most relevant memory and graph context for the current task. |
| Principles | Keeps work aligned with the project’s operating commitments. |
| Audit | Checks risky actions, code changes, and governance boundaries. |
| Trust | Adjusts autonomy according to observed reliability and review outcomes. |
| Skills | Encapsulates repeatable work patterns such as session start, remembering, and closure. |
| Dispatch | Routes larger or specialized work through the Hunter Guild task lifecycle. |
| Packs and extensions | Move reusable experience or optional capabilities between environments. |

## 4. Memory lifecycle

Memory is treated as a living layer, not a static archive.

```text
capture -> classify -> embed -> deduplicate -> score -> retrieve -> reinforce or decay
```

Useful memories become easier to retrieve when they are repeatedly relevant. Weak, duplicated, or stale memories are merged or decay over time. This keeps the system from becoming a pile of old notes that drown out current truth.

SQLite is the canonical truth source. Updates that affect retrieval-visible
content or availability pass through a field-scoped source-mutation transaction:

```text
validate server authority and declared project
  -> reload canonical source and peer rows
  -> recheck canonical project equality
  -> patch only owned ordinary-memory fields
  -> write lineage and stale dependent synthesis
  -> bump canonical memory version
  -> enqueue checked LanceDB upsert/delete jobs
  -> commit
  -> repair the derived index
```

This boundary covers public update/correct/forget calls, smart duplicate
replacement, feedback, skills, RecMem, lifecycle scans, duplicate cleanup, GC
merge, and internal audit rollover. A project or authority mismatch fails before
partial canonical state becomes visible. Metadata-only patches stay narrow and
do not rewrite fields owned by provenance, summary, lifecycle, or index policy.

Governed synthesis and memory proposals are opt-in. Only current `verified`
synthesis with complete actor/call/time evidence can enter recall; source changes
make dependents stale in the same SQLite transaction. Pending, rejected, and
expired proposals never enter ordinary recall or LanceDB. The default gates
preserve legacy behavior, and rollback disables the gates without deleting
canonical control or audit records.

Structured indexing uses deterministic `structure-v1` manifests implemented in
both Python and Rust. SQLite retains canonical memory text and exact manifest
material; LanceDB remains a rebuildable projection. Each bounded chunk carries a
stable parent memory ID, heading path, block kind, source span, content hash, and
truncation state. Dashboard memory and lineage views validate the manifest hash
and source identity before exposing chunk anchors.

Retrieval explanation is a persisted, bounded projection rather than a replay of
the retrieval engine. It records channel scores, ranking/filter decisions,
pipeline counts, chunk evidence, and measured request/stage durations. Missing
timing evidence remains absent, so operators can distinguish unavailable data
from a genuine sub-millisecond measurement.

## 5. Skill and agent orchestration

Skills define reusable operating rituals. Agent dispatch extends those rituals across multiple workers.

The high-level rule is simple: an agent should never work blind. Before delegation, it should receive relevant memory, active principles, and enough task context to avoid repeating known mistakes.

The orchestration layer is intentionally chain-based:

```text
clarify -> research -> isolate work -> plan -> execute -> test -> review -> finish
```

The exact implementation can vary by client or agent, but the principle remains the same: work should move through visible stages rather than hidden ad-hoc action.

## 6. Hunter Guild model

The Hunter Guild is the project’s task routing metaphor.

- Work is posted as a commission.
- Agents claim work according to trust and capability.
- Progress is kept alive through heartbeat and trace state.
- Completed work is reviewed before it becomes accepted system state.
- Rejection, timeout, or abandonment affects future autonomy.

This model turns distributed AI work into a governed queue instead of an untracked pile of prompts.

## 7. Git governance

The release flow favors a clean public history:

- Public release work targets `main` unless a maintainer explicitly chooses another integration branch.
- Release repositories should contain only source, public documentation, tests, and reproducible configuration.
- Runtime files, generated exports, local agent state, private implementation notes, and heavy design drafts should stay out of the public release tree.
- Pull requests should be reviewable, conventionally named, and merged only after explicit maintainer approval.

Live `release-sync.py` is fail-closed: the release repository must be clean, on
`main`, bound to the expected `origin`, and missing the current version tag both
locally and remotely. After validation it stages only the computed release
paths and rejects all other staged, unstaged, or untracked changes. Run a
dry-run first. The first and only live invocation must include `--push`; the
same process commits, creates the annotated tag, revalidates pinned object IDs
and remote state, then atomically pushes `main` and the exact tag. A live run
without `--push` leaves local release state that blocks a clean retry. Never
replace this attested path with a manual push or `git push --tags`.

```bash
python scripts/release-sync.py --from <base>..<merged> --audit-range <base>..<merged> \
  --version v0.1.19 --release-repo F:/Agent/plastic-promise-release \
  --expected-source-branch main --validation-profile full --dry-run
# Repeat with the same bound origin arguments and --push only after all gates pass.
```

## 8. Public release boundary

The public release repository should show the system’s purpose and safe operating model, not every internal planning artifact.

Include:

- README and user-facing docs.
- Source code required to run the system.
- Tests and reproducible setup files.
- High-level architecture and governance documents.

Exclude:

- Runtime logs, PID files, caches, and generated archives.
- Local IDE or agent configuration.
- Private worktree state.
- Detailed internal planning/specification archives that are not needed for users.
- Temporary diagnostic scripts not part of the supported workflow.

## 9. Runtime mode boundary

The HTTP `/health` response is a deployment identity record, not only a
liveness probe. It includes `pid`, `source_root`, `source_revision`,
`fusion_policy`, and `fusion_attestation`; the latter uses
`retrieval-fusion-identity/v1` and binds the requested policy, candidate ID, and
configuration hash. A newly spawned MCP server must match its launcher PID and
source root, plus the expected Git revision when available. An existing process
on port 9020 is reused only when the same checkout identity matches.

On Windows, `python scripts/init_and_start.py --stop` consumes only
`var/run/mcp_server.pid` and `var/run/maintenance_daemon.pid` from the current
checkout and verifies the command line's source root. It never uses a global
Python-process scan, so a sibling worktree is outside the stop boundary.

The one-click launcher can start the system in five explicit modes:

| Mode | Boundary |
|---|---|
| `light` | Fast bootstrap; LanceDB startup work is deferred and Python context supply is forced. |
| `normal` | Python context supply with LanceDB available through lazy initialization. |
| `rust-normal` | Rust-first context supply with Python fallback, without startup LanceDB rebuild. |
| `full` | Python context supply plus startup LanceDB init/backfill/rebuild. |
| `rust-full` | Rust-first context supply plus full startup LanceDB maintenance. |

Interactive launcher runs ask for the mode when `--mode` is omitted. Non-interactive runs default to `rust-full`, preserving the most complete Rust-first path for automation. A running MCP process can be inspected or changed with `runtime_mode(action="get")` and `runtime_mode(action="set", mode="light")`; the server refreshes Rust health and heavy initialization state after a change.

For `full` and `rust-full`, LanceDB backfill/rebuild is startup warmup owned by the launcher. In the long-running MCP process, request-time heavy initialization should open LanceDB/domain backends while leaving `LDB_BACKFILL_ON_INIT=0` and `LDB_REBUILD_ON_INIT=0`, so `context_supply` and `memory_recall(debug=true)` stay out of maintenance work on the hot path.

`context_supply` runs synchronous context assembly on a bounded worker pool
with configurable embedding and supply deadlines. A deadline returns an
auditable degraded result instead of blocking the MCP HTTP event loop. The Rust
snapshot path batches LanceDB vector reads only for memory IDs that already
passed canonical admission; it must not use an unrestricted table scan or
reintroduce one query per memory.

Rust context supply is currently a snapshot accelerator, not the persistent
storage authority. Python still owns SQLite/LanceDB, code-memory enrichment,
rerank, and context-gate behavior, then passes a per-call memory/vector snapshot
to Rust for ranking, fusion, filtering, and layering. Debug responses should
include filter-stage counts, stage timing, fallback reason, and per-item
keep/drop reasons so operators can explain why a memory entered or missed the
result set. The `rust_snapshot_supply` benchmark tracks the full Python-to-Rust
snapshot boundary and should be used for p50/p95 regression gates.

The local Dashboard V2 reads these persisted projections without invoking the
retrieval engine again. Its overview, memory, request, synthesis, lineage,
retrieval-explain, operations, trust, and configuration routes share the same
loopback and server-owned project boundary. Enable it with `PP_DASHBOARD_V2=1`;
enable its bounded explain route separately with `PP_RETRIEVAL_EXPLAIN=1`.

## 10. Degraded-mode boundary

Plastic Promise is local-first by default. Optional external calls depend on configured agents, embedding providers, rerankers, or LLM integrations. If optional services are unavailable, the runtime should explicitly label degraded behavior and continue through safe fallback paths when possible.

The default vector model and the default rerank model serve different roles.
Ollama `mxbai-embed-large` is used for local embeddings; long memory text is
chunked, mean-pooled, and normalized before indexing so launcher warmup can keep
SQLite and LanceDB in sync without exceeding the embedding context window.
Local reranking uses a generation-capable Ollama model (`qwen2.5:3b`) and falls
back to cosine/original ordering when model output is unavailable or invalid.

## 11. Operating principles

Operational verification uses `maintenance_daemon.py --once --json` for one
production-equivalent cycle and `smoke_restart_recovery.py --artifact-dir ...
--json` for cross-process checked-index recovery. Daemon health is attested by
the PID-bound `maintenance-heartbeat/v1` record. Existing `memory-index/v2`
upserts remain replay-compatible, while all new ordinary index writes use V3.

Retrieval fusion stays on `legacy-auto` unless a frozen manifest selects an
exact `wrrf-v1:<sha256>` candidate; `max-v1` is the comparison baseline and
invalid or mismatched configuration fails closed. Rollback restores
`legacy-auto`, unsets the RRF K/weight/window variables, retains canonical
SQLite/outbox evidence, and replays the default policy before HTTP and restart
smokes.

The `0.1.15` one-shot public calibration produced no eligible WRRF candidate.
Held-out queries therefore remained unopened, `legacy-auto` remains the release
policy, and this release makes no fusion-improvement claim.

1. **Context before action** — retrieve relevant memory before major decisions.
2. **Scoped heavy context** — pass `stage_session_id`, `flow_line_id`, and `request_id` to concurrent `memory_recall` / `context_supply` calls so the derived `request_scope_id` isolates cache and audit state and remains visible in `context_supply` output.
3. **Debug parity on hot paths** — in `rust-full`, `memory_recall(debug=true)` and Rust-backed `context_supply(debug=true)` should keep the Rust snapshot path when Rust is healthy, returning debug counters without forcing a Python full-pipeline detour; when LanceDB rows exist, `pipeline_stats.vector_count` should be nonzero.
4. **Traceability over speed** — leave a path future agents can audit.
5. **Small reversible steps** — prefer changes that are easy to review and undo.
6. **Explicit degradation** — if a subsystem is unavailable, say so and use a safe fallback.
7. **No blind delegation** — subagents must receive context and principles.
8. **Verification before completion** — done means checked, not merely edited.
9. **Reflection after output** — useful lessons should feed the next loop.

## 12. Minimal mental model

Plastic Promise is a loop:

```text
remember -> retrieve -> act -> verify -> reflect -> remember better
```

Everything else exists to keep that loop reliable as the project grows.
