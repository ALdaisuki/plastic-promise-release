# Plastic Promise — System Chain Overview

> Release-facing overview. This document describes the system shape and operating principles without exposing private planning artifacts.

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
| Launcher runtime modes | Select startup depth and Rust acceleration before services start, with MCP hot updates through `runtime_mode`. |
| Service bootstrap | Launcher child processes inherit runtime-mode env and receive project root on `PYTHONPATH`; the Maintenance Daemon also bootstraps `_project_root` for direct script starts. |
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

## 10. Degraded-mode boundary

Plastic Promise is local-first by default. Optional external calls depend on configured agents, embedding providers, rerankers, or LLM integrations. If optional services are unavailable, the runtime should explicitly label degraded behavior and continue through safe fallback paths when possible.

## 11. Operating principles

1. **Context before action** — retrieve relevant memory before major decisions.
2. **Scoped heavy context** — pass `stage_session_id`, `flow_line_id`, and `request_id` to concurrent `memory_recall` / `context_supply` calls so the derived `request_scope_id` isolates cache and audit state and remains visible in `context_supply` output.
3. **Debug parity on hot paths** — in `rust-full`, `memory_recall(debug=true)` should keep the Rust snapshot path when Rust is healthy, returning debug counters without forcing a Python full-pipeline detour; when LanceDB rows exist, `pipeline_stats.vector_count` should be nonzero.
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
