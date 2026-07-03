# Plastic Promise — System Chain Overview

> Release-facing overview. This document describes the system shape and operating principles without exposing internal implementation plans.

## 1. What this system is

Plastic Promise is a memory-centered coordination system for AI-assisted software work. It connects memory, context retrieval, principles, audit, task dispatch, and feedback loops into one operating chain.

The goal is not to store everything forever. The goal is to keep useful commitments alive, let stale context decay, and make each action traceable enough that future agents can continue work safely.

## 2. Core chain

```text
Session start
  -> context supply
  -> principle alignment
  -> task execution
  -> verification
  -> step closure
  -> memory update
  -> future context
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
| Memory | Stores reusable experience, decisions, preferences, and task knowledge. |
| Context | Retrieves the most relevant memory and graph context for the current task. |
| Principles | Keeps work aligned with the project’s operating commitments. |
| Audit | Checks risky actions, code changes, and governance boundaries. |
| Trust | Adjusts autonomy according to observed reliability and review outcomes. |
| Skills | Encapsulates repeatable work patterns such as session start, remembering, and closure. |
| Dispatch | Routes larger or specialized work to agents through the Hunter Guild model. |
| Packs | Exports reusable experience so knowledge can move between agents or environments. |

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

- `Dev` is the development line.
- Release repositories should contain only source, public documentation, tests, and reproducible configuration.
- Runtime files, generated exports, local agent state, private implementation notes, and heavy design drafts should stay out of the public release tree.
- Pull requests should be reviewable, conventionally named, and merged only after explicit approval.

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
- Detailed internal planning/specification archives.
- Temporary diagnostic scripts not part of the supported workflow.

## 9. Operating principles

1. **Context before action** — retrieve relevant memory before major decisions.
2. **Traceability over speed** — leave a path future agents can audit.
3. **Small reversible steps** — prefer changes that are easy to review and undo.
4. **Explicit degradation** — if a subsystem is unavailable, say so and use a safe fallback.
5. **No blind delegation** — subagents must receive context and principles.
6. **Verification before completion** — done means checked, not merely edited.
7. **Reflection after output** — useful lessons should feed the next loop.

## 10. Minimal mental model

Plastic Promise is a loop:

```text
remember -> retrieve -> act -> verify -> reflect -> remember better
```

Everything else exists to keep that loop reliable as the project grows.
