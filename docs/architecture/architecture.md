# Plastic Promise — Architecture Reference

> Release-facing architecture reference.
> Last updated: 2026-08-07.

## 1. System Overview

Plastic Promise is a local-first MCP runtime for AI agent memory, context supply, audit, trust, skills, and governed task dispatch. It is built around **Commitment Engineering**: operating agreements become retrievable context, traceable decisions, and feedback loops instead of only external enforcement rules.

- **Purpose**: Help AI agents act with memory, principles, verification, and traceable autonomy.
- **Primary users**: Claude Code, MCP clients, agent teams, and maintainers operating local governance workflows.
- **Current tool surface**: 58 MCP tools declared in `plastic_promise/mcp/server.py`, including compatibility aliases.
- **Primary storage**: SQLite WAL is the canonical truth source; LanceDB is a
  rebuildable derived vector/text retrieval index.
- **Acceleration path**: optional Rust `context-engine-core`; Python remains the canonical write/full fallback pipeline and applies a final recall-noise guard to Rust results. In `rust-full`, normal recall and `memory_recall(debug=true)` stay on the Rust snapshot hot path while Rust is healthy.

## 2. Architecture Diagrams

- [diagrams/c4-level1-context.txt](diagrams/c4-level1-context.txt) — C4 Level 1 context.
- [diagrams/c4-level2-container.txt](diagrams/c4-level2-container.txt) — C4 Level 2 containers.
- [diagrams/c4-level3-component.txt](diagrams/c4-level3-component.txt) — C4 Level 3 memory/context path.
- [diagrams/architecture.mermaid](diagrams/architecture.mermaid) — Full container diagram.
- [diagrams/sequence.mermaid](diagrams/sequence.mermaid) — Multi-agent sequence.
- [diagrams/components.mermaid](diagrams/components.mermaid) — Component breakdown.
- [distribution-profiles.svg](distribution-profiles.svg) — Standard distribution deployment profiles.
- [distribution-profiles.zh-CN.svg](distribution-profiles.zh-CN.svg) — Chinese distribution profile overview.
- [three-endpoint-deployment/architecture.md](three-endpoint-deployment/architecture.md) — Three-endpoint target architecture.
- [three-endpoint-deployment/architecture.zh-CN.md](three-endpoint-deployment/architecture.zh-CN.md) — Chinese three-endpoint target architecture.

### Target endpoint packaging

The staged deployment target is three deep endpoint modules:

| Endpoint module | Target responsibility | State authority |
|---|---|---|
| `pp-local-edge` | Dashboard, Deployment Center, MCP bridge, and bounded local projection. | No canonical authority. |
| `pp-server-backend` | MCP, governance, durable work, Maintenance, routing, and generation control. | Sole canonical SQLite writer and persistent runtime-state aggregator. |
| `pp-compute-node` | Typed embedding, rerank, and optional fixed-schema inference. | Derived results and model cache only. |

LanceDB remains a rebuildable derived generation owned and promoted by the
backend. The frontend invokes only validated host `ppctl` plans; it receives no
Docker socket, SSH private key, or arbitrary shell interface. This is a target
for the six stacked PRs and is not evidence that production has migrated.

## 3. Runtime Containers

| Container | Source area | Responsibility |
|---|---|---|
| MCP Server | `plastic_promise/mcp/` | Tool schemas, tool routing, stdio/SSE entrypoints, health endpoints, dashboard, prompts, and resources. |
| Context Engine | `plastic_promise/core/context_engine.py` | Builds task context from vector, text, symbolic, graph, principle, worth, and decay signals. |
| Memory Pipeline | `plastic_promise/memory/`, `plastic_promise/memory/pipeline.py` | Extracts, classifies, deduplicates, scores, embeds, persists, reinforces, merges, and decays memories. |
| Knowledge System | `plastic_promise/knowledge/` | Owns source-grounded knowledge records and rebuildable derived indexes. The current LanceDB slice is a gated, project-wide shadow builder only; worker/CLI scheduling and promotion remain deferred. |
| Storage Layer | SQLite + `plastic_promise/core/lancedb_store.py` + `plastic_promise/core/generation_live_index.py` | Persists canonical records, tasks, trust, and graph metadata in SQLite. LanceDB generations and their generation-bound live views provide derived vector/search indexes. |
| Trust and Defense | `plastic_promise/defense/`, `plastic_promise/core/step_auditor.py` | Applies hard boundaries, trust tiers, audit reports, and pre-action checks. |
| Governance Runtime | `plastic_promise/core/tool_manifest.py`, `plastic_promise/core/event_protocol.py`, `plastic_promise/core/mgp_shadow.py`, `plastic_promise/core/context_recommender.py` | Adds explainable tool manifests, unified runtime events, MGP shadow semantics, and recommendation metadata without replacing SQLite truth sources. |
| Skills | `plastic_promise/skills/`, `plastic_promise/core/workflow_state.py`, `plastic_promise/loop/` | Implements session lifecycle, smart remembering, step closure, and immutable generations of the pinned Matt Pocock workflow adapter. |
| Hunter Guild | `plastic_promise/mcp/tools/task_queue.py`, `plastic_promise/core/task_*` | Coordinates task enqueue, claim, heartbeat, completion, verification, and penalties. |
| Persistent Collaboration Runtime | `plastic_promise/collaboration/`, `plastic_promise/deployment/collaboration_schema_migration.py` | Current PR5 source provides server-only durable schema/stores, restart-safe authenticated Hook continuation, server-owned bounded work issuance/operations, ordinary tool reconcile, bounded Stop progress/submitted events, formal result/stage receipts, Maintenance composition, shadow/inject awareness, read-only Dashboard collaboration projections, and atomic accepted-result promotion into a pending-only outbox. Live browser/runtime smoke, migration execution, and production activation remain unverified. |
| Maintenance Daemon | `daemons/maintenance_daemon.py`, `plastic_promise/cron/` | Runs lifecycle scans, scheduler health checks, memory decay scans, trust scans, quality scans, and bounded passive semantic/promotion job processing. The script bootstraps the project root for direct execution. |
| Launcher | `scripts/init_and_start.py`, `plastic_promise/launcher/` | Starts MCP server, daemon, watchdog, environment checks, and bootstrap checks. Child services inherit runtime-mode environment and receive the project root at the front of `PYTHONPATH`. |
| Local Inference Node | `plastic_promise/local_inference_node/`, `deploy/local-inference-node/` | Optional loopback-only local heterogeneous inference process. Each registration declares model name, fixed revision, output dimension, normalization/strategy, artifact hash and transport evidence; it returns bounded derived results only and has no SQLite, LanceDB, MCP, queue, or canonical-memory write capability. |
| Release Delivery | `plastic_promise/release_builder/`, `deploy/release-builder/`, `deploy/server/`, `deploy/local-inference-node/`, `.github/workflows/release-*.yml`, `plastic_promise/release_manifest.py` | A maintainer creates a secret-free, immutable request and a 30-minute desktop confirmation. The local Builder first validates the exact `D:\PlasticPromise\remote-builds\<SHA>\source` workspace and completes a 10-second read-only resource gate; busy hardware exits without Docker cleanup, Buildx creation, queueing, or retry. PR verification is no-push; protected stable jobs bind a full source SHA, produce immutable GHCR digests, CycloneDX SBOM and provenance, and write a non-overwritable manifest. Server deployment consumes verified digests only; SQLite migration, LanceDB promotion and Maintenance remain independently selected request actions. |
| Node Governance | `plastic_promise/core/node_governance.py` | Server-owned registration, identity-drift quarantine, conservative capacity accounting, project-scoped derived-task leases, retries, reconciliation, identity-bound scheduling, and the safe Dashboard projection for remote nodes, Ollama, cloud, and local structural fallback. |
| Deployment Controller | `plastic_promise/deployment/`, `scripts/plastic-promise-deploy.*` | Cross-platform, local-only plan/preflight/SQLite lifecycle controller. It owns only an explicitly selected state root, its installer record, canonical SQLite and verified local backups; it never starts services, manages Docker/Compose, opens tunnels, or gains provider/node canonical-write authority. |
| Extensions | `plastic_promise/extensions/`, `plugins/` | Loads validated optional packs and external capability adapters. |
| Rust Core | `rust/context-engine-core/` | Optional context-engine acceleration path. Snapshot ingestion filters audit telemetry before BM25/FTS/vector indexing, while Python still guards the native result boundary. |

### Deployment-controller data flow

```text
secret-free deployment manifest
  -> deterministic profile/module resolution
  -> operation-bound plan: action + installer-state + SQLite/WAL/SHM
     fingerprints, plus restore-source digest when applicable (no side effects)
  -> read-only disk/preflight and platform doctor
  -> explicitly selected local state root
       -> empty SQLite: create once, integrity_check, versioned migration
       -> existing SQLite: attach, integrity_check, verified online backup,
          versioned migration
       -> installed upgrade/repair: existing SQLite is mandatory; a missing
          primary must enter the separately confirmed restore/replace-db path
       -> restore/replace-db: explicit same-profile source + confirmation +
          service-stopped acknowledgement + pre-restore backup + staged
          WAL/SHM rollback if primary replacement fails
  -> installer-owned state record and local verified backups
```

The controller is not a runtime supervisor. Systemd, launchd, Windows service
management, Compose, node process lifecycle, reverse-tunnel activation and all
remote calls stay outside this data flow. The doctor can inspect command
availability, local disk/state, and explicitly supplied redacted node/tunnel/
runtime evidence, but does not reveal endpoints, identity values, model paths
or credentials. SQLite remains canonical throughout; LanceDB is not read or
modified by the deployment controller.

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
    +--> session-init ---------------------> authenticated Agent-session registration
    |                                        + bounded working-set / peer delta / cursor
    +--> step-closure ---------------------> formal collaboration ResultReceipt
    |                                        + optional memory proposal kept separate
    |
    +--> sp-stage(no receipt) -------------> Pinned Skill execution contract
    |        Agent runs the named Codex Skill
    +--> sp-stage(execution receipt) ------> Receipt validation
             -> governance adapter
             -> deterministic outer/composite-child lifecycle entities
             -> atomic SQLite receipt + workflow cursor commit

UserPromptSubmit Hook
    |
    +--> deterministic route/authority gate
    +--> optional bounded cloud JSON classification for model-authority routes
    +--> active workflow instance resolution
             -> unfinished generation resumes
             -> completed generation remains immutable history
             -> explicit new root creates a new generation

Stop Hook
    |
    +--> explicit rule candidate ----------> canonical proposal transaction
    +--> rule miss ------------------------> durable semantic classification job
             -> Hook returns before cloud inference
             -> isolated worker batch
             -> grounded pending proposal

SessionEnd Hook
    |
    +--> resumes the exact durable AgentSession with a short-lived,
         server-issued continuation assertion
    +--> closes the session, releases active leases, records cursor state,
         revokes continuation digests, then removes local state
    +--> deferred/invalid continuation keeps local retry state

Maintenance Daemon
    |
    +--> direct script bootstrap inserts project root for imports
    +--> scans SQLite state, task queues, trust, memory decay, scheduler health
    +--> creates or updates tasks through the same governed lifecycle
    +--> replays passive proposal outbox, processes semantic jobs, reconciles
         eligible promotion work, processes promotion jobs, then expires proposals
    +--> replays checked post-watermark index outbox jobs into the active
         generation-bound live view; never mutates the verified generation
```

The PR 5 collaboration Maintenance reconcile algorithm is present as a source
slice, but this diagram does not claim its production scheduling or activation.
Likewise, the current formal result path assigns the submitter role on the
server and preserves a durably submitted result when later reflection degrades;
that source behavior is not a substitute for Hook/runtime E2E evidence.

## 6. Memory and Context Data Flow

```text
memory_store(content)
  -> smart extraction
  -> category/tier classification
  -> vector embedding
  -> duplicate detection
  -> QualityGate scoring
  -> Weibull decay initialization
  -> SQLite canonical write + durable checked index outbox
  -> generation runtime: Maintenance replays only jobs newer than the
     reconciled generation watermark into its private live view

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

Heavy `memory_recall` and `context_supply` calls accept `stage_session_id`, `flow_line_id`, and `request_id`. The MCP handlers derive `request_scope_id`, attach it to audit metadata, render it in `context_supply` output, and use it to keep overlapping official workflow stages, sub-agent dispatches, and recall cache entries isolated.

The `UserPromptSubmit` Hook emits one bounded advisory workflow block. Every non-empty fallback keeps the exact project, session, flow, and route identifiers in one `WorkflowScope` value object. Scope values are XML-text escaped before rendering, so an identifier cannot close or forge the envelope. The fallback prioritizes scoped `session-init` and executable `sp-stage` calls over optional route detail, preserves a 300-character project ID under the default route budget, and never emits a partial XML-like contract. User-only Skills require a positive command at the start of the prompt. Questions, negations, status statements, and mentions do not become user attestations. A single `GeneralTaskIntent` parser selects positive command clauses from otherwise untyped Hook text before assigning a reachable model route. Read-only explanations, status statements, and negated clauses without a later positive action stay on `routing`; trailing negative scope constraints do not erase earlier affirmative work.

This local parser is intentionally a bounded, fail-closed grammar rather than a claim to understand arbitrary natural language. An optional structured-JSON provider may run in shadow or enrich model-route classification, but provider output remains untrusted, cannot mint user-only attestations, and degrades to `routing/ask-matt` on timeout, invalid JSON, low confidence, or provider failure. Deterministic commands never wait for that provider.

Official workflow state separates the client conversation lane from immutable run generations. A completed generation remains queryable history but cannot control a later task. An unfinished active generation resumes when no different root is selected; an explicit accepted root supersedes it and allocates a new generation-specific flow line. Route candidates remain advisory until the deterministic authority gate accepts them.

Passive semantic capture is also fail closed and asynchronous. A Stop Hook rule miss creates a durable job and returns without waiting for cloud inference. Batches are partitioned by project, visibility, configuration revision, and provider identity. Worker output must be strict grounded JSON derived only from original user-authored text; it may merge or split facts without crossing a partition. Eligible proposals enqueue separate durable promotion work. Lease tokens and fencing generations protect completion, stable reason codes record retry/dead outcomes, and reconciliation recreates missing eligible work without changing promotion policy. `evaluate_auto_promotion()` remains the sole policy authority. Dashboard V2 projects only project-scoped job metadata and aggregate status; payloads, results, user text, and foreign-project jobs never enter the passive-memory response.

`sp-stage` does not execute a Codex Skill. A first call returns a contract pinned to the upstream revision and `SKILL.md` hash. After the client runs that Skill, a second call supplies a non-secret caller attestation. The governance adapter records receipt-scoped deterministic lifecycle entities before `official_workflow_receipts` and `official_workflow_state` are committed together in a short SQLite transaction. Composite receipts declare their actual child calls in `evidence.invoked_skills`; those child entities are marked `composite_receipt`, not presented as independent Hook observations. Deterministic IDs make post-adapter retries entity-idempotent, while the single-writer production rule remains the transaction boundary.

Context recommendation metadata is advisory. It explains why already-eligible memories, tools, or principles were ranked, but it does not reintroduce hard-excluded context or override project policy.

### Generation-bound derived index

A verified LanceDB generation is immutable and remains the reproducible base
artifact. An operator may bootstrap a private live root from that exact
generation only when its manifest contains reconciled outbox evidence and the
receipt still verifies against canonical SQLite. The live manifest binds the
base generation ID, manifest digest, outbox watermark, embedding identity, and
the retained one-time activation ID selected by `current`. Promotion and
rollback first create `selections/<activation-id> -> ../generations/<id>` with
exclusive-create semantics, fsync it, and then atomically replace `current` with
a link to that selection. Selection links are never deleted or reused. Runtime
startup rejects missing evidence, legacy direct pointers, mismatched bindings,
unsafe paths, or an old live root after promotion or rollback, including an
A -> B -> A cycle.

Python and Rust retrieval resolve the same live index. Maintenance can mutate
only this copy and selects checked `memory_index` and `synthesis_index` jobs with
`rowid` greater than the bound watermark. Runtime refresh does not run a full
index synchronization over the live view. Health, control-plane status, and the
Dashboard expose bounded lag counts; blocked, unknown, or unavailable lag
degrades vector readiness instead of claiming the derived index is current.
SQLite remains authoritative throughout, and replacing or deleting a live root
is a separate operator-authorized action.

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
| Derived retrieval index | Verified LanceDB generation + generation-bound live view | The generation is immutable. The live copy is bound to its manifest and receives only checked post-watermark outbox replay. |
| Trust scores | SQLite | Persisted in `trust_scores` and history tables. |
| Task queue | SQLite | Hunter Guild lifecycle tables. |
| Runtime events | SQLite `runtime_events` | Unified pending/running/completed/error events for tool calls, task transitions, and MGP shadow evaluations. |
| Official workflow instances, cursor, and receipts | SQLite `official_workflow_instances`, `official_workflow_state`, `official_workflow_receipts` | Immutable run generations plus project/session/flow-isolated cursors and validated caller attestations; receipts and cursor changes commit atomically per route step. |
| Passive semantic and promotion work | SQLite derived-work jobs + memory proposal tables | Durable partitioned jobs, leases, retry/dead reasons, proposal observations, and reconciliation state. Pending proposals are not canonical memory. |
| Inference-node governance | Server SQLite `inference_nodes`, short-lived node reservations, latency samples, audit events, existing `derived_work_jobs`, and a daily-deduplicated accelerator decision ledger | Stores non-secret identity/transport evidence bound to a server-issued resolved-manifest and active-controlled-revision proof, health/capacity, reservation evidence, latency samples, and identity-drift/recovery audit events. MCP and Maintenance bootstrap read the active control store read-only and resolve tunnel endpoints only from a protected server-private runtime file; endpoints and authorization never enter revisions, receipts, public status, or logs. Durable index replay uses `DerivedWorkStore`; foreground rerank uses the same short-lived verified selection but retains live query/candidate text only in process memory. Nodes themselves do not open canonical tables. Execution-time identity drift triggers persistent quarantine before the next selection, and only a later matching health observation recovers it. `accelerator-max` uses the existing outbox plus a durable UTC-day admission ledger for low-priority non-generative artifacts, yielding to foreground embedding/rerank work. Its Dashboard audit combines redacted lifecycle records with bounded denied/deferred decision events; the latter are daily-deduplicated audit records, never a task queue. Both exclude project, subject, payload, result, provider, transport, and lease fields. The additive v2 registry schema is only applied through an explicit backup-backed controlled migration; runtime can report a missing schema but cannot create it. |
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
| Local inference defaults | Development-friendly Ollama defaults may be configured, but production routing is bound to the registered model/revision/dimension/normalization/artifact identity rather than a model family name alone. |
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
| Persistent collaboration runtime | Source implemented / focused tests passed / runtime evidence pending | Durable schema/stores, restart-safe authenticated Hook continuation, server-owned bounded work issuance/operations, ordinary tool reconcile, bounded Stop progress/submitted events, formal result/stage receipts, replay/idempotency/fencing, Maintenance composition, shadow/inject awareness, read-only collaboration UI, and atomic pending-only promotion enqueue exist. Real browser/runtime smoke, migration execution, and production evidence remain outstanding. |
| Skills and official workflow | Active | Pinned routes, generation-isolated Hook injection, deterministic invocation authority, optional model-route classification, and the compatibility stage entrypoint are exposed. |
| Passive semantic proposal pipeline | Experimental, off by default | Rule misses can enqueue isolated cloud JSON classification and governed promotion work; Dashboard V2 reports bounded project-scoped queue/retry/dead state without payloads; all gates fail closed and SQLite remains canonical. |
| Node observability | Active | The authenticated loopback control plane exposes a safe, bounded node Dashboard projection, a planning-only resource preflight view, and an explicit browser-local diagnostic download built from a separate strict allowlist. These surfaces do not expose endpoints, evidence, identities, user payloads, paths, values, or secrets, and none can mutate deployment state. |
| Extension market | Experimental | Pack validation and market commands exist; ecosystem is early. |
| Release pipeline | Release-ready | PR builds are no-push. RC, TestPyPI, stable GHCR evidence, release-repository sync, and PyPI are separately manual protected workflows. Stable evidence binds one full source SHA, exact Python artifacts, SBOM hashes, and immutable OCI digests. |

## 11. Scalability Notes

- SQLite WAL is sufficient for local agent teams with many readers and a small number of writers.
- LanceDB keeps vector indexes disk-backed and suitable for larger memory pools than in-memory search. Immutable generations provide reproducible rebuild points; generation-bound live views absorb checked incremental work without rewriting the base artifact.
- The daemon performs lifecycle detection without LLM calls; LLM cost belongs to agent reasoning, extraction fallback, or configured external providers.
- Optional semantic classification workers batch cloud calls outside SQLite write transactions. Partition keys prevent unrelated projects, visibility scopes, configurations, or providers from sharing one request.
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
