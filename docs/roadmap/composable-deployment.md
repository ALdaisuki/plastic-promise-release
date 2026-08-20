# Composable deployment roadmap

Chinese parity page:
[`composable-deployment.zh-CN.md`](composable-deployment.zh-CN.md).

> **Normative scope:** the machine-readable
> [`union-six-pr-contract.json`](../standards/union-six-pr-contract.json),
> revision `2026-08-18.1`, is the authoritative six-PR delivery contract. This
> roadmap is a non-normative implementation projection. A PR is complete only
> when every `delivery_scope`, `collaboration_scope`, and `required_evidence`
> item passes; a current source slice never proves the whole PR complete or a
> runtime/production state.

## Status legend

- **Current**: a pure contract or compatibility capability that this repository
  can verify with focused tests.
- **Legacy current**: an existing V1 path that does not silently gain V2
  cross-provider or production-promotion authority.
- **Target**: an image, executor, migration, or release action implemented by a
  later PR.
- **Unverified**: no runtime evidence was available in this worktree; it must
  not be described as accepted production behavior.

## Now — stage one

- PR 1 `routing-core`: governed foreground retrieval embedding routing, health
  semantics, immutable collaboration value contracts, a project/session-scoped
  append-only `CollaborationEventLog` adapter foundation, and strict
  `project_id` isolation for existing task paths. It does not yet provide a
  persistent agent registry/work board, a new mutable collaboration-lease
  lifecycle, Hook/MCP wiring, awareness injection, or memory promotion.
- PR 2 `endpoint-contracts`: **current source-only** `EndpointAuthority`, a pure
  deep Module with `resolve` -> `assess` -> `verify_completion`.
  `EndpointContractRegistry` remains compatibility naming where source callers
  retain it. The server compiles a closed `EndpointAuthorityProfile`:
  `pp-local-edge` has intent plus bounded-read authority, `pp-server-backend` is
  the sole canonical-state, inference-job-scheduling/validation,
  collaboration, and governance writer/decision owner, and `pp-compute-node`
  alone executes bounded inference leases and returns derived result/evidence.
  `project_id`, manifest/hello claims, and advertised capabilities never grant
  authority. This PR does not activate a runtime, transport, persistence or
  migration, start containers, enable Maintenance, or promote LanceDB.

Legacy-current paths include the V1 manifest, `/v1/identity`,
`/v1/embeddings`, `/v1/rerank`, existing node governance, and the durable
outbox. Matching vector dimensions alone never make a V1 identity compatible
with a V2 index generation.

## Project coordination and memory boundary

Project-level multi-agent collaboration is part of the same six-PR delivery
line, but it uses three separate planes:

- **Coordination Plane:** server-owned, project-scoped agent presence, work
  leases, typed events, review/conflict receipts, and cursor-based history;
- **Project Working Set:** a bounded, rebuildable projection of the current
  plan, active work, candidate result references, blockers, and peer deltas;
  an artifact is accepted only when bound to the server-authenticated,
  independent `AcceptanceReceipt` defined by PR1-C06;
- **Canonical Memory:** stable facts, decisions, principles, and lessons. Only
  an accepted evidenced result may create a pending proposal, and adoption is
  still a separate governed action.

Peer progress, heartbeats, transient blockers, assumptions, raw prompts, and
hidden reasoning are not promotion-eligible by default. Agent agreement alone
does not turn a finding into a project fact.

PR 1 implements the immutable/event-log/isolation foundation and PR 4 adds the
pure working-set/awareness source contracts. The current PR 5 source/focused-test
slice adds the durable collaboration runtime, server-owned work issuance,
ordinary tool reconcile, bounded Stop progress/submitted events, atomic
accepted-result-to-pending-only promotion enqueue, and compute-node-only
embedding/rerank/structured-JSON execution with project-scoped
`local`/`cloud`/`hybrid` routing. Structured JSON is disabled by default. Live
browser/runtime lifecycle, mutable migration execution, provider activation,
production acceptance, and publication remain unverified. Delivery assignment:

- PR 1 adds immutable project/session/agent/work/event/result/cursor values,
  the append-only event-log foundation, and strict project isolation for the
  existing task lifecycle; it does not add persistent `AgentRegistry` or
  `ProjectWorkBoard` services;
- PR 2 keeps inference compute jobs and developer-Agent collaboration work
  separate even when `project_id` matches; compute receives no Task Queue,
  `AgentRegistry`, work-board, event-writer, awareness, memory/knowledge-
  promotion, merge, deployment, Maintenance, or LanceDB-promotion authority;
- PR 3 packages the PR 1 foundation only in the server role; edge and compute
  artifacts physically contain no collaboration package. It does not activate
  persistence;
- PR 4 adds current source-level `ProjectWorkingSet` and role-aware
  `AgentAwarenessProjection` read views. Each source class is capped at 64
  items; the projection accepts at most 20 delta events and 64 KiB of canonical
  JSON. `project_for(*, audience: AgentSession, deltas: EventPage)` is only a
  non-authoritative value projection: a role string or caller-created session
  grants no visibility. Full-work consumption requires a server-authenticated
  active session, current policy claim, source-lineage/cursor/projection digest,
  and independent `AcceptanceReceipt` lineage. Other authorized roles receive
  only own, audience-visible event, and dependency work. Cursor scope,
  progression, source identity, policy/factory revision, and event audience
  fail closed; work objectives, Agent capabilities, event payloads, prompts,
  private reasoning, credentials, and result bodies are redacted. The
  projection has no canonical-memory effect, and runtime feed binding remains
  deferred;
- PR 5 current source adds server-only durable registry/work-board/session/
  lease/event/result/acceptance stores, restart-safe authenticated Hook
  continuation, server-owned bounded work issuance/operations, ordinary tool
  reconcile, bounded Stop progress/submitted events, formal result/stage
  receipts, replay/idempotency/fencing checks, Maintenance composition,
  shadow/inject awareness, read-only Dashboard projections, and atomic
  accepted-result-to-pending-only promotion enqueue. It also confines local,
  hosted, and raw embedding/rerank/structured-JSON providers to
  `pp-compute-node`, with structured JSON off by default and project-scoped hot
  routing. Real browser/runtime smoke, mutable migration execution, provider
  activation, and production evidence remain unverified.
  `CollaborationMemoryPromoter` may only emit a pending proposal for
  independently accepted, evidenced, conflict-resolved work and cannot adopt
  it;
- PR 6 verifies role packaging, upgrade/rollback compatibility, documentation
  parity, and the final cross-agent E2E.

## PR 3 source boundary — stage two

- PR 3 `container-artifacts`: **current source-level**
  `ContainerArtifactCompiler` policy and inspectable descriptors for
  edge/standard, backend/standard, compute/CPU, and compute/CUDA. It validates
  immutable OCI/SBOM/provenance evidence and static recipe policy, including
  exclusions for models, SQLite, LanceDB, secrets, and runtime state. Protected
  CI is configured for no-push OCI build verification; the compiler does not
  activate a host/container runtime, create a tunnel, publish a registry, or
  make a deployment/production change. Its collaboration addition packages the
  PR 1 event-log foundation as server-only; edge and compute receive no writer
  authority, and no collaboration persistence is activated.
- PR 4 `deployment-center`: **current source-level** local-edge Deployment
  Center/host `ppctl` planning contracts plus bounded `ProjectWorkingSet` and
  role-aware `AgentAwarenessProjection`. The awareness contract is rebuildable,
  redacted, cursor-scoped, and non-authoritative; it does not persist state or
  bind a server feed, `AgentRegistry`, `ProjectWorkBoard`, Hook, MCP, or runtime.
- PR 5 `migration-operations`: current source includes backup-gated migration
  contracts, server-owned manifest/receipt persistence, persistent agent/work
  state, the mutable lease/session lifecycle, MCP/Hook wiring, shadow awareness,
  pending-only promotion orchestration, and compute-node-only
  embedding/rerank/structured-JSON contracts. Live SQLite migration, LanceDB
  shadow promotion, Maintenance cutover, rollback execution, provider
  activation, and production evidence remain target/unverified operations.
- PR 6 `release-readiness`: cross-platform installer entries, RC bundles,
  profile/cross-agent E2E, final documentation parity, role-capability evidence,
  protected release preparation, and Workflow Composer as shadow-only
  candidate planning with the fixed route retained as execution authority.
  Public PyPI, GHCR stable, GitHub Release, and release-repository publication
  still require separate authorization.

## Delivery cadence

Each PR runs only its smallest deterministic seam tests and bilingual
documentation gate, then obtains independent Standards, Spec, and DeepSec
Shield/code-smell receipts bound to the same immutable source, diff,
requirement set, and union-contract revision. Only then may it merge in
dependency order after explicit authorization. A runtime-affecting merge gets a
reversible compatibility or shadow deployment slice before the next PR begins.
The full cross-profile, cross-agent, migration, recovery, and rollback E2E runs
after PR6 as final acceptance; it should not be the first place basic scope,
schema, or authority defects are found.

## Explicit non-goals for this stack

- Multiple canonical SQLite writers, SQLite replication, or writable state on a
  compute node.
- Treating `project_id`, manifest/hello claims, or advertised capabilities as an
  authority grant.
- Arbitrary agent prompts, shell/file execution, or MCP administration on a
  compute node.
- Automatic merge of unreviewed PRs, public publication, or production changes.
- Treating peer activity, intermediate reasoning, or a pending proposal as
  canonical memory.

See [`../deployment/README.md`](../deployment/README.md) and the
[three-endpoint architecture](../architecture/three-endpoint-deployment/architecture.md)
for profile, model-identity, and ownership detail.
