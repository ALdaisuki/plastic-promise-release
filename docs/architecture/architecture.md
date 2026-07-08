# Plastic Promise — Architecture Reference

> Release-facing architecture reference.
> Last updated: 2026-07-08.

## 1. System Overview

Plastic Promise is a local-first MCP runtime for AI agent memory, context supply, audit, trust, skills, and governed task dispatch. It is built around **Commitment Engineering**: operating agreements become retrievable context, traceable decisions, and feedback loops instead of only external enforcement rules.

- **Purpose**: Help AI agents act with memory, principles, verification, and traceable autonomy.
- **Primary users**: Claude Code, MCP clients, agent teams, and maintainers operating local governance workflows.
- **Current tool surface**: 58 MCP tools declared in `plastic_promise/mcp/server.py`, including compatibility aliases.
- **Primary storage**: SQLite WAL for structured state and LanceDB for vector/text retrieval.
- **Acceleration path**: optional Rust `context-engine-core`; Python remains the canonical write/full fallback pipeline and applies a final recall-noise guard to Rust results. In `rust-full`, normal recall and `memory_recall(debug=true)` stay on the Rust snapshot hot path while Rust is healthy.

## 2. Architecture Diagrams

- [diagrams/c4-level1-context.txt](diagrams/c4-level1-context.txt) — C4 Level 1 context.
- [diagrams/c4-level2-container.txt](diagrams/c4-level2-container.txt) — C4 Level 2 containers.
- [diagrams/c4-level3-component.txt](diagrams/c4-level3-component.txt) — C4 Level 3 memory/context path.
- [diagrams/architecture.mermaid](diagrams/architecture.mermaid) — Full container diagram.
- [diagrams/sequence.mermaid](diagrams/sequence.mermaid) — Multi-agent sequence.
- [diagrams/components.mermaid](diagrams/components.mermaid) — Component breakdown.

## 3. Runtime Containers

| Container | Source area | Responsibility |
|---|---|---|
| MCP Server | `plastic_promise/mcp/` | Tool schemas, tool routing, stdio/SSE entrypoints, health endpoints, dashboard, prompts, and resources. |
| Context Engine | `plastic_promise/core/context_engine.py` | Builds task context from vector, text, symbolic, graph, principle, worth, and decay signals. |
| Memory Pipeline | `plastic_promise/memory/`, `plastic_promise/memory/pipeline.py` | Extracts, classifies, deduplicates, scores, embeds, persists, reinforces, merges, and decays memories. |
| Storage Layer | SQLite + `plastic_promise/core/lancedb_store.py` | Persists records, tasks, trust, graph metadata, and vector/search indexes. |
| Trust and Defense | `plastic_promise/defense/`, `plastic_promise/core/step_auditor.py` | Applies hard boundaries, trust tiers, audit reports, and pre-action checks. |
| Governance Runtime | `plastic_promise/core/tool_manifest.py`, `plastic_promise/core/event_protocol.py`, `plastic_promise/core/mgp_shadow.py`, `plastic_promise/core/context_recommender.py` | Adds explainable tool manifests, unified runtime events, MGP shadow semantics, and recommendation metadata without replacing SQLite truth sources. |
| Skills | `plastic_promise/skills/`, `plastic_promise/loop/` | Implements session lifecycle, smart remembering, step closure, and SuperPowers stage integration. |
| Hunter Guild | `plastic_promise/mcp/tools/task_queue.py`, `plastic_promise/core/task_*` | Coordinates task enqueue, claim, heartbeat, completion, verification, and penalties. |
| Maintenance Daemon | `daemons/maintenance_daemon.py`, `plastic_promise/cron/` | Runs lifecycle scans, scheduler health checks, memory decay scans, trust scans, and quality scans. The script bootstraps the project root for direct execution. |
| Launcher | `scripts/init_and_start.py`, `plastic_promise/launcher/` | Starts MCP server, daemon, watchdog, environment checks, and bootstrap checks. Child services inherit runtime-mode environment and receive the project root at the front of `PYTHONPATH`. |
| Extensions | `plastic_promise/extensions/`, `plugins/` | Loads validated optional packs and external capability adapters. |
| Rust Core | `rust/context-engine-core/` | Optional context-engine acceleration path. Snapshot ingestion filters audit telemetry before BM25/FTS/vector indexing, while Python still guards the native result boundary. |

## 4. Agent and Actor Inventory

| Actor | Role | Primary interface |
|---|---|---|
| Human developer | Sets goals, reviews changes, approves merges, configures runtime. | Git, CLI, MCP client, browser dashboard. |
| AI coding agent | Uses memory/context/audit tools before acting. | MCP stdio or SSE. |
| Agent team | Builder/fixer/reviewer style workers in governed workflows. | MCP tools, task queue, HTTP/SSE optional bridge. |
| Maintenance daemon | Non-LLM lifecycle automation and scans. | SQLite, MCP health endpoint, local process management. |

## 5. Communication Patterns

```text
Human / Agent
    |
    v
MCP Server (stdio or SSE)
    |
    +--> memory_recall / context_supply --> Request scope --> Context Engine --> SQLite + LanceDB
    |
    +--> audit_pre_check / defense -------> TrustStore + Audit + Tool Manifest
    |
    +--> mgp_shadow_bridge ---------------> MGP policy mapping + runtime_events
    |
    +--> task_enqueue / task_claim --------> Hunter Guild tables
    |
    +--> session-init / step-closure ------> Skill Engine + Memory Pipeline

Maintenance Daemon
    |
    +--> direct script bootstrap inserts project root for imports
    +--> scans SQLite state, task queues, trust, memory decay, scheduler health
    +--> creates or updates tasks through the same governed lifecycle
```

## 6. Memory and Context Data Flow

```text
memory_store(content)
  -> smart extraction
  -> category/tier classification
  -> vector embedding
  -> duplicate detection
  -> QualityGate scoring
  -> Weibull decay initialization
  -> SQLite + LanceDB write

context_supply(task)
  -> request_scope_id from stage_session_id + flow_line_id + request_id
  -> principle activation
  -> vector/text/symbolic/graph retrieval
  -> Rust snapshot hot path for rust-full normal and debug recall
  -> recall-noise guard before scoring and at the Rust/Python boundary
  -> rank fusion and optional rerank
  -> context recommender annotations
  -> worth/decay adjustment
  -> core, related, divergent context package
```

Heavy `memory_recall` and `context_supply` calls accept `stage_session_id`, `flow_line_id`, and `request_id`. The MCP handlers derive `request_scope_id`, attach it to audit metadata, render it in `context_supply` output, and use it to keep overlapping SuperPowers stages, sub-agent dispatches, and recall cache entries isolated.

Context recommendation metadata is advisory. It explains why already-eligible memories, tools, or principles were ranked, but it does not reintroduce hard-excluded context or override project policy.

## 7. Trust and Error Handling

| Layer | Mechanism | Trigger | Action |
|---|---|---|---|
| L0 hard boundary | `audit_pre_check` / enforcer | Dangerous or forbidden operation | Block and record trust impact. |
| L1 trust constraint | `defense(action="get")` / `defense(action="evaluate_tool")` | Trust below required tier or tool manifest risk requires review | Restrict action, ask for approval, or explain allow/ask/deny. |
| L2 immune patrol | Audit and daemon scans | Periodic health or quality issues | Report, enqueue repair, or degrade explicitly. |
| Task timeout | Hunter Guild heartbeat | Missing heartbeat | Release, escalate, or penalize according to lifecycle rules. |
| Degraded mode | fallback flags and explicit status | Optional subsystem unavailable | Continue through safe fallback and label uncertainty. |

## 8. Storage and State

| State | Storage | Notes |
|---|---|---|
| Memories | SQLite + LanceDB | Structured metadata plus vector/text search. |
| Trust scores | SQLite | Persisted in `trust_scores` and history tables. |
| Task queue | SQLite | Hunter Guild lifecycle tables. |
| Runtime events | SQLite `runtime_events` | Unified pending/running/completed/error events for tool calls, task transitions, and MGP shadow evaluations. |
| Runtime logs | `var/log/` | Local runtime output; not part of public docs. |
| Runtime PIDs/heartbeats | `var/run/` | Used by launcher and daemon. |
| Service import path | child-process `PYTHONPATH` + daemon `sys.path` bootstrap | Keeps launcher-managed and direct daemon starts aligned with source checkout imports. |
| Experience packs | JSON exports | Portable knowledge bundles. |

## 9. Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.10+, optional Rust PyO3 core |
| Protocol | Model Context Protocol over stdio and Streamable HTTP |
| Vector store | LanceDB |
| Structured database | SQLite WAL |
| Default local embedding | Ollama `mxbai-embed-large`, with chunked long-text pooling and fallback embedder path |
| Default local reranker | Ollama `qwen2.5:3b`, with cosine/original-order fallback |
| Web runtime | Starlette + uvicorn |
| Tests and quality | pytest, ruff, mypy, pre-commit |
| Packaging | setuptools, PyPI metadata in `pyproject.toml` |

## 10. Status Matrix

| Area | Status | Notes |
|---|---|---|
| MCP server | Active | stdio and Streamable HTTP modes are implemented; legacy SSE endpoints remain available. |
| Memory pipeline | Active | Extraction, quality gate, LanceDB write, and decay are implemented. |
| Context supply | Active | Python remains full fallback and write-side authority; heavy calls carry request-scope metadata for concurrent flow isolation. |
| Rust context core | Experimental | Optional acceleration path; `rust-full` keeps normal and debug recall on Rust snapshot while Rust is healthy, with audit-telemetry filtering at snapshot ingestion and Python conversion. |
| Hunter Guild | Experimental | Lifecycle tools exist; scanner policy and SNR are evolving. |
| Skills and SuperPowers | Active | Programmatic tools and stage entrypoint exposed. |
| Extension market | Experimental | Pack validation and market commands exist; ecosystem is early. |
| Release pipeline | Active | Release sync and PyPI publishing are configured. |

## 11. Scalability Notes

- SQLite WAL is sufficient for local agent teams with many readers and a small number of writers.
- LanceDB keeps vector indexes disk-backed and suitable for larger memory pools than in-memory search.
- The daemon performs lifecycle detection without LLM calls; LLM cost belongs to agent reasoning, extraction fallback, or configured external providers.
- The launcher owns subprocess environment normalization. It prepends the project root to `PYTHONPATH`, while the daemon script also self-bootstraps `_project_root` for direct starts.
- Context quality depends on explicit degraded-mode labeling when optional services are unavailable.

## 12. Security and Privacy Boundary

Plastic Promise is local-first by default. Memories, trust, and task state are stored locally unless the operator configures external agents, hosted embedding providers, hosted rerankers, or other network integrations.

Security posture:

- Validate MCP tool inputs.
- Use parameterized database operations.
- Run audit and trust checks before risky actions.
- Keep runtime logs, PID files, caches, and private agent state out of release artifacts.
- Do not merge pull requests without explicit maintainer authorization.
