# Plastic Promise Three-Endpoint Deployment Architecture

> Status: staged architecture, 2026-08-17. It records current source contracts
> through the PR 5 migration seam and target runtime deployment work; it does not
> claim that production has migrated, that a listener is running, or that any
> external runtime state is verified. Current PR 5 collaboration evidence is
> source/test evidence only; it performs no production runtime verification.

> **Normative scope:** this document is subordinate to
> [`union-six-pr-contract.json`](../../standards/union-six-pr-contract.json),
> revision `2026-08-18.1`. Every PR is complete only when every
> `delivery_scope`, `collaboration_scope`, and `required_evidence` item passes;
> neither half may be reported as full PR completion, and source/test evidence
> is not runtime/production evidence.

Chinese parity document:
[`architecture.zh-CN.md`](architecture.zh-CN.md).

## Status legend

| Label | Meaning in this document | Evidence / exclusion |
|---|---|---|
| **current** | Source-level contract present in the relevant stacked PR worktree. | Not a listener, built/loaded image, persisted record, or production rollout. |
| **legacy-current** | Existing repository/runtime path retained as the compatibility baseline. | Not an observation that any particular host is healthy or connected. |
| **target** | Work intentionally assigned to PR 3–PR 6. | Not installed, released, or operational. |
| **unverified** | External runtime state was not proven in this task. | No runtime conclusion follows from source contracts alone. |

## 1. System overview

Plastic Promise has a target topology of three independently placeable endpoint
modules. PR 2 provides the pure, source-only `EndpointAuthority` Module and its
versioned endpoint contracts. The Module compiles a closed
`EndpointAuthorityProfile` on the server and exposes one small Interface:
`resolve` -> `assess` -> `verify_completion`. `EndpointContractRegistry`
remains a compatibility name where retained by source callers. PR 3 adds the
source-level build/inspection policy for endpoint artifacts; endpoint
processes, placement, transport, persistence, and all runtime application
remain later work. PR 4 adds current source-level `ProjectWorkingSet` and
`AgentAwarenessProjection` contracts, but no server feed, collaboration
persistence, Hook/MCP binding, or awareness runtime is active.

| Endpoint module | Contract boundary in PR 2 | Runtime status |
|---|---|---|
| `pp-local-edge` | Intent submission and bounded read projection only; no canonical, collaboration, governance, compute-persistence, or deployment authority. Its base artifact policy is inspectable in PR 3. | **target** static browser entry at `http://127.0.0.1:19021`, container start, and placement; it does not host MCP. |
| `pp-server-backend` | Sole canonical Task Queue, collaboration, memory/knowledge-proposal, and governance writer and decision owner, including SQLite, inference-job scheduling, routing, retries, reconcile, accepted-result validation, receipts, and LanceDB-promotion decisions. It never constructs or executes a provider. | **current** pure authority/ownership resolution plus server-side scheduling/transport contracts; **target** mounts, persistence, MCP/runtime operations, and promotion. |
| `pp-compute-node` | Bounded inference lease plus derived `embedding`, `rerank`, and fixed-schema `structured-json` result/evidence only. CPU/CUDA and local/cloud/hybrid variants stay behind this same contract. | **current** protocol/identity/admission/lease contracts and isolated node-service source; **target** verified placement, runtime transport, and production evidence. |

The endpoint split follows deployment responsibility rather than decomposing
the server into user-visible microservices. A future pure local installation
may run all three endpoint modules as separate containers on one host. A split
installation will change placement and transport, not ownership.

Target success means:

- only `pp-server-backend` can mount or write canonical SQLite;
- Dashboard and Deployment Center use `pp-local-edge` on loopback at
  `http://127.0.0.1:19021`; that browser entry serves static content only;
- the existing `http://127.0.0.1:19020/mcp` entry belongs to
  `pp-server-backend`, not `pp-local-edge`;
- `pp-compute-node` receives bounded inference leases and returns only derived
  results and evidence; it cannot access files, shell, MCP administration, or
  canonical SQLite, and it cannot acquire Task Queue, `AgentRegistry`,
  work-board, collaboration-event-writer, awareness, memory/knowledge-promotion,
  merge, deployment, Maintenance, or LanceDB-promotion authority;
- inference compute jobs and developer-Agent collaboration work remain separate
  planes even when both carry the same `project_id`;
- one secret-free Deployment Manifest is the deployment truth source;
- embedding identity cannot drift across local and cloud providers;
- loss of optional inference degrades explicitly without stopping canonical
  writes or pretending derived state is current;
- project-scoped agent coordination is isolated from long-term memory: peer
  progress is visible through bounded collaboration projections but is not
  promoted merely because another agent reported it;
- every rollout, fallback, rebuild, promotion, and rollback is traceable.

PR 2 declares the parts of these invariants that are representable in a pure
endpoint authority contract: closed role/action profiles, server-only
ownership, versioned capability and identity evidence, admission,
leases/fencing, and sanitised record schemas. `project_id`, manifest or hello
claims, and advertised capability strings are inputs to validation; none grants
authority.
PR 3 adds a separate build-time contract: a secret-free request resolves a
role/platform/variant matrix and is materialized only into inspectable artifact
descriptors. Neither PR enforces an operating-system mount, starts a service,
persists a record, or performs a LanceDB promotion.
PR 2 specifically activates no runtime, transport, persistence or migration,
deployment, Maintenance, or promotion path.

## 2. Architecture diagrams

- [`diagrams/architecture.txt`](diagrams/architecture.txt): compact ASCII
  map with **current / legacy-current / target / unverified** labels, limited
  to 100 columns.
- [`diagrams/workflow.mermaid`](diagrams/workflow.mermaid): V2 contract flow
  plus **target** deployment/configuration adapters.
- [`diagrams/sequence.mermaid`](diagrams/sequence.mermaid): **target** recall
  degradation/recovery flow; PR 2 supplies only its typed contract shapes.
- [`diagrams/artifact-build.txt`](diagrams/artifact-build.txt) and
  [`diagrams/artifact-build.mermaid`](diagrams/artifact-build.mermaid): PR 3
  source-level artifact-plan/materialization boundary, explicitly separate from
  runtime authority.
- [`diagrams/container-artifact-matrix.svg`](diagrams/container-artifact-matrix.svg)
  and its Chinese-equivalent SVG: visual artifact matrix. They remain design
  evidence, not evidence that an image exists on a host.

## 3. Module inventory and seams

The three endpoints are deep modules. PR 2 puts the authority Seam in the pure
`EndpointAuthority` Module. Its Interface is only `resolve`, `assess`, and
`verify_completion`; role/action resolution, claim distrust, and completion
denial stay hidden in its implementation. `EndpointContractRegistry` remains a
compatibility name where source callers still import it. PR 3 adds the separate
`ContainerArtifactCompiler` Interface:
`prepare(request) -> ArtifactBuildPlan` and
`materialize(plan, executor) -> ArtifactBundle`. Docker/Compose, HTTP/gRPC,
SSH, scheduling, persistence, and deployment application remain adapter
concerns, not imports of either deep module.

For backward readability, `authorities_for()` and the browser `authorities`
field keep the earlier descriptive labels. The new closed, enforceable matrix
is exposed separately as `EndpointAuthorityProfile.actions` and the additive
browser `actions` field; callers must not treat descriptive labels as grants.

| Module | External interface | Hidden implementation | Invariant |
|---|---|---|---|
| `EndpointAuthority` | **current source-only** `resolve` -> `assess` -> `verify_completion`; returns a server-compiled `EndpointAuthorityProfile` and typed decisions. | Closed role/action matrix, ownership resolution, capability/identity comparison, lease/fence checks, and fail-closed rejection reasons. | `project_id`, manifest/hello claims, and advertised capabilities never grant authority; only the compiled profile can admit an action. |
| `ContainerArtifactCompiler` | **current** source-level `prepare` / `materialize` build seam. | Matrix resolution, OCI policy, image-recipe selection, executor differences, and descriptor inspection. | An artifact plan/bundle cannot start a container or authorize deployment. |
| `pp-local-edge` | **current source / target runtime** static browser entry at `http://127.0.0.1:19021` and Deployment Center projection. | Static browser content, bounded session cache, and a default-disabled no-store bridge-configuration asset. It hosts no MCP or host proxy. | Browser/cache state is never deployment or runtime truth and has no host-operation authority. |
| Host `ppctl` planning adapter | **current source contract / target runtime binding** closed `inspect` / `preview` planning adapter; execution is unavailable (`deferred_to_pr5`). | Host inspection, profile recommendation, V2 validation, plan/preflight shaping, redaction, and later operational adapters. | The edge receives no Docker socket, arbitrary host socket, SSH private key, path, SQLite, or arbitrary command interface. |
| `ProjectWorkingSet` / `AgentAwarenessProjection` | **current PR 4 contract plus PR 5 durable binding**: immutable working set and authenticated durable feed composition. | Same-project/session validation, bounded source/event pages, role/audience filtering, explicit cursor acknowledgement, shadow/inject gating, and field redaction. | The projection is non-authoritative. Source and focused tests cover session registration, continuation, feed composition, and cursor resume; real runtime/production evidence remains pending. |
| Server-owned `MigrationOperation` orchestrator | **current PR 5 durable source contract / target live phase adapters**; no live adapter is active or verified in this document. | Typed runtime/node/canonical-state/derived-index phase adapters, Migration Operation Plan, Execution Grant, SQLite grant/lease/fence journal, rollback and durable secret-free receipt. | Production composition gives durable migration-lease and canonical-write authority only to `pp-core`/`pp-server-backend` and must use the SQLite journal; the in-memory journal is test/non-production only. Deployment Center/`ppctl` remain read-only. |
| `pp-server-backend` | **current** resolved authority owner; **target** MCP/control/query and compute-job adapters. | SQLite transactions, durable inference jobs, routing decisions, leases, retries, reconcile, accepted-result validation, collaboration/governance writes, receipts, Maintenance, and generation selection. | It is the sole canonical-state, inference-job scheduling/validation, collaboration, and governance writer/decision owner; provider execution is forbidden. |
| Project coordination fabric | **current source implementation / focused tests passed / runtime evidence pending**: PR 1–PR 4 foundations plus PR 5 durable server-only runtime slices. | Agent/session/role/plan/work/lease/activity/event/cursor/result/acceptance persistence; restart-safe authenticated Hook continuation; server-owned bounded work issuance and operations; ordinary tool-call reconcile; Stop progress/submitted events; formal stage/result receipts; Maintenance composition; shadow/inject awareness; read-only Dashboard projections; and accepted-result promotion into a pending-only outbox. | Coordination state is project-scoped; projections remain rebuildable and non-authoritative, promotion can emit only a pending proposal, and no source/test slice grants canonical-memory or production authority. |
| `pp-compute-node` | **current** bounded inference authority profile plus typed protocol/identity/admission contract; **target** service. | CPU/CUDA runtime, batching, model cache, resource evidence, and derived-result shaping. | It can lease inference and return derived evidence only; it receives no collaboration, governance, promotion, merge, deployment, or canonical-write authority. |
| Inference adapter seam | **current** declared `embedding`, `rerank`, and fixed-schema `structured-json` capabilities; hosted providers execute only inside `pp-compute-node`. | Local, cloud, or hybrid compute node; the backend only schedules registered nodes, validates identity, and persists results. | All adapters obey one capability schema and identity policy; the server never invokes a provider or assembles inference context. |
| Transport adapter seam | **target** authenticated private endpoint transport. | Docker network, restricted SSH/reverse SSH, and future private transports. | Application interfaces do not depend on transport choice. |

### Generation preparation and cutover boundary

The current operator seam keeps derivation work, host lifecycle authority, and
Control mutation distinct:

```text
Prepare plane
  quality evidence -> build -> reconcile -> verify inactive candidate
    -> atomic prepare receipt (manifest/index tree + quality + staged revision digests)

Independent host lifecycle boundary
  stop MCP / inference gateway / Maintenance / Knowledge Ingest

Cutover plane
  optional authenticated Control revision activation
    -> promote with the exact revision environment
    -> authenticated generation retarget
    -> generation-bound live-root bootstrap + verification
    -> atomic runtime pointer update

Independent post-cutover boundary
  restart -> health/retrieval smoke -> separate Maintenance review/transition
```

The cutover tool resolves canonical SQLite and generation-root paths from the
runtime EnvironmentFile unless explicitly overridden, drops privileged
generation commands to the runtime owner, and uses Bearer + CAS Control APIs.
Cutover recomputes the manifest bytes, declared manifest/index-tree identity,
quality evidence, and staged revision digest before any mutation, so neither
the candidate nor its revision EnvironmentFile can silently change between phases.
It does not write Control SQLite directly, restart services, change Maintenance
policy, or manufacture quality evidence. Source and focused tests prove this
operator contract; they are not claims that a production cutover occurred.

The deletion test justifies both adapters: removing the inference adapter would
spread provider selection and identity checks across recall, indexing, and
Maintenance; removing the transport adapter would spread platform networking
logic across all three endpoints.

The compiler is deliberately a third seam rather than a deployment helper. If
it were removed, role/variant rules and secret/state exclusions would drift
between Dockerfiles, CI workflows, and later host adapters. Its full matrix,
mount policy, and build-versus-runtime boundary are recorded in
[`container-artifacts.md`](container-artifacts.md).

PR 4 keeps the Deployment Center deep rather than turning the browser into a
deployment orchestrator: the host module has two public operations,
`DeploymentCenter.inspect(installation_ref)` and
`DeploymentCenter.preview(DeploymentPreviewRequest)`. The `ppctl` executable
is only the host-side typed planning dispatcher for those operations; it is not
a general command runner. Execution is unavailable in PR 4
(`deferred_to_pr5`). This is a **current source contract / target runtime
deployment**: no listener, host binding, or runtime deployment is active or
verified by this documentation.

PR 5 adds a **current durable source / target live-adapter** server-owned migration seam, not
a browser apply path. Its `MigrationOperation` binds a short-lived Migration
Operation Plan to
fresh canonical-state, artifact, runtime, node, and derived-index evidence, then
requires a separate operation-bound Execution Grant before any mutation. The
Deployment Center inspection hash cannot be promoted into that grant. Typed
phase adapters may perform only preflight, backup/rehearsal, cutover, shadow
rebuild/promotion, Maintenance transition, rollback, and receipt persistence;
they never receive arbitrary shell, Docker, SSH, or SQLite commands. Until the
production composition is independently authorised, no listener, container,
tunnel, migration, LanceDB promotion, Maintenance transition, or MCP restart is
verified. The source defaults to a 300-second plan/grant TTL (900-second maximum)
and rejects observations older than 120 seconds; mutable apply requires the
server-memory topology/artifact bindings and rejects digest-only projections.
The canonical SQLite journal persists issued grants, installation-scoped
leases, monotonic fences, one-shot operation states, and secret-free receipts.
Its tables are installed only by the backup-gated versioned deployment
migration. Expired running work becomes `recovery-required`; stale owners fail
completion CAS. Live phase adapters and runtime activation remain target work.

### Project-level multi-agent collaboration planes

> Status: **current source implementation / runtime evidence pending / target
> production activation**. In addition to the PR 1 foundation and PR 4
> projections, PR 5 source now contains server-only durable collaboration
> schema/stores, authenticated cross-transport Hook continuation, bounded
> work-board operations, formal stage/result receipts, Maintenance composition,
> shadow/inject awareness, read-only Dashboard topology/work/timeline, and
> accepted-work promotion validation/outbox, server-owned work issuance,
> ordinary tool-call reconcile, and bounded typed `Stop` progress/submitted
> events. Accepted work is now atomically enqueued into the pending-only
> promotion outbox. It does not yet prove real browser/runtime lifecycle smoke
> or activate the migration and Maintenance lifecycle in production. No source or test
> slice grants a delegated agent canonical-memory, deployment, database, or
> production authority, and none completes PR 5 or PR 6.

The collaboration model deliberately separates live coordination from durable
project memory:

| Plane | Purpose and lifetime | Authority and memory rule |
|---|---|---|
| Coordination Plane | Project-scoped agent presence, intent, work leases, typed progress/finding/blocker/artifact events, and review receipts. Events are retained only as long as operational and audit policy requires. | Server-owned append-only authority. A peer event is evidence about collaboration state, not a project fact or instruction. |
| Project Working Set | A **current source contract** for a rebuildable snapshot of the current goal, plan revision, active presence/work, candidate result references, blockers, conflicts, and cursor-supplied peer deltas. Each of its five source classes is capped at 64 items. | It owns no adapter or database. A result is not accepted merely because it is completed or carries artifact references; accepted-artifact projection requires the server-authenticated independent `AcceptanceReceipt` defined by PR1-C06. `AgentAwarenessProjection` remains non-authoritative and declares `canonical_memory_effect: none`. |
| Canonical Memory | Stable project facts, decisions, principles, failure lessons, and architecture constraints that remain useful across sessions. | Only an accepted, evidenced result may become a pending memory proposal; governed adoption is still required before it becomes canonical memory. |

The source/runtime split is:

```text
[current PR 5 source] authenticated session + durable collaboration stores
    -> typed event / formal result -> CollaborationEventLog / result store
    -> [current source] ProjectWorkingSet -> AgentAwarenessProjection
    -> server policy + source lineage + independent AcceptanceReceipt verification
    -> [current source/test] accepted result validation
       -> atomic pending-only CollaborationMemoryPromoter outbox enqueue
    -> pending memory proposal -> governed adoption -> Canonical Memory

[current source/test] authenticated Hook continuation + bounded work-board lifecycle
[current source/test] shadow/inject awareness + read-only Dashboard projections
[current source/test] server-owned work issuance + ordinary tool reconcile
    + Stop progress/submitted events + automatic pending-only promotion enqueue
```

`ProjectWorkingSet` accepts no more than 64 agent sessions, leased-work
receipts, accepted-result receipts, blockers, or conflicts per class. A
projection accepts no more than 20 peer-delta events and refuses canonical JSON
larger than 64 KiB. A role is only a visibility input to the non-authoritative
value projection. Full active-work visibility is consumable only after the
server binds the exact active `AgentSession` to a current least-privilege policy
claim and verifies the event-page source lineage, cursor tuple, projection
digest, and any independent `AcceptanceReceipt`; a caller-created
coordinator/reviewer session or role string grants nothing. Other authorized
roles receive only their own work, audience-visible event work, and
dependencies of their own work. Project/session/audience mismatch, cursor
regression or gaps, stale policy/factory revision, source substitution, forged
pages/digests, and acceptance-receipt mismatch all fail closed. Work objectives,
Agent capabilities, event payloads, raw prompts, private reasoning,
credentials, and result bodies are redacted. The remaining summaries, IDs,
digests, evidence references, and cursors are coordination evidence only,
never canonical memory or authority.

`work.progressed`, heartbeats, transient blockers, unverified assumptions, raw
prompts, and hidden reasoning are never promotion-eligible by default. A
finding remains typed as a finding until its evidence is reviewed; agreement
between agents does not by itself turn it into a fact.

Delivery is distributed through the existing six-PR line:

| Delivery slice | Collaboration responsibility |
|---|---|
| PR 1 `routing-core` | Add immutable project/session/agent/work/event/result/cursor value contracts, the append-only `CollaborationEventLog` foundation, and strict `project_id` isolation for every existing task enqueue/dedupe/claim/heartbeat/complete/review/inbox/release/recovery path. This is not a persistent registry or new mutable work board. |
| PR 2 `endpoint-contracts` | Keep inference compute jobs separate from developer-Agent collaboration even when `project_id` matches. Deny compute Task Queue, `AgentRegistry`, work-board, event-writer, awareness, memory/knowledge-promotion, merge, deployment, Maintenance, and LanceDB-promotion authority. |
| PR 3 `container-artifacts` | Package the PR 1 collaboration foundation only in the server role; prove edge artifacts receive at most read-projection contracts and compute artifacts receive no collaboration authority. It does not activate persistence or listeners. |
| PR 4 `deployment-center` | Add current source-level bounded `ProjectWorkingSet` and `AgentAwarenessProjection` value contracts, plus the required authenticated-feed boundary. `project_for(*, audience: AgentSession, deltas: EventPage)` alone grants no authority; a server-verified session/policy/source/cursor/projection/acceptance tuple is required before any full-work view is consumable. |
| PR 5 `migration-operations` | Current source adds server-only durable collaboration schema/stores, restart-safe authenticated Hook continuation, server-owned bounded ProjectWorkBoard issuance/operations, ordinary tool reconcile, bounded Stop progress/submitted events, formal result/stage receipts, Maintenance composition, shadow/inject awareness, read-only Dashboard collaboration projections, and atomic accepted-result-to-pending-only promotion enqueue. Real browser/runtime smoke, migration execution, production activation, and governed runtime/production evidence remain unverified. |
| PR 6 `release-readiness` | Verify role packaging, upgrade/rollback compatibility, bilingual documentation parity, and the final cross-agent end-to-end acceptance flow. |

## 4. Deployment profiles

> Status: **target runtime profiles**. PR 2 validates the V2 profile names and
> endpoint-role constraints; it does not install, inspect, or run a profile.

| Deployment Profile | Placement | Active embedding identity preference |
|---|---|---|
| `local-all-in-one` | All three endpoint containers on one host. | Local managed compute runtime. |
| `local-cloud` | Local edge and backend on one host; configured cloud inference may be used. | Cloud identity. |
| `split-accelerated` | Local edge on the user machine, backend on the server, compute on a separate local host. | Local compute-node identity. |

The installer may recommend a profile after read-only inspection of operating
system, Docker/WSL2, CPU, memory, GPU, disk, network, and existing models. The
user confirms or adjusts the recommendation before a manifest is resolved.
Insufficient required disk space is a hard preflight failure.

### Logical time policy

All three endpoint processes and the browser projection use `TZ=UTC`. Canonical timestamps,
lease/fencing comparisons, receipts, and release evidence remain timezone-aware UTC values.
Deployment assets set only the process/container environment and the browser formatter: they do
not mount `/etc/localtime`, call `timedatectl`, call Windows `Set-TimeZone`, or otherwise modify a
host timezone. This keeps local edge, server backend, native client, and compute-node displays
consistent while leaving every Linux, macOS, and Windows host unchanged.

## 5. Deployment Manifest and update flow

PR 2 provides `EndpointManifestV2` and a deterministic resolved-plan digest as
**current** pure contracts. The operational manifest authority, frontend,
quick-install scripts, `ppctl`, and update flow below are **target** adapters;
none is active or verified here. In particular, the PR 5 migration path is
server-owned: `pp-core` is the sole canonical SQLite writer, while the browser
and `ppctl` remain read-only planning surfaces.

```text
frontend choices
  -> secret-free EndpointManifestV2 candidate
  -> [current source / target runtime PR 4] host ppctl inspect / preview
  -> safe resolved-plan, manifest-diff, identity, and preflight projection
  -> inspection-only plan hash + classified future update
  -> [current source / target runtime PR 5] fresh Migration Operation Plan
     + separate Execution Grant
  -> [current source / target runtime PR 5] server-owned typed migration operation
  -> [target PR 5 adapter] persisted secret-free Deployment/Migration Receipt
```

`ManifestRevisionRecord` and `DeploymentReceipt` are **current** server-owned,
sanitised schemas; PR 2 does not persist them. Secrets are stored through host
credential adapters or server secret storage only in the **target** runtime.
The manifest records references, never secret values. **PR 6 target work** adds
a Release Bundle that binds an immutable source revision, profile/variant and
protocol compatibility matrix, OCI digest evidence, SBOM/provenance references,
and an opaque Model Catalog reference/digest. The Model Catalog describes fixed
model revisions, artifact hashes, capabilities, resource estimates, and
compatible runtimes. These are target evidence requirements, not a claim that a
bundle, signature, registry artifact, or model admission already exists.

### Tiered hot updates

| Change class | Apply mechanism | Examples |
|---|---|---|
| Live apply | Update active runtime policy without restart. | Provider priority, polling, circuit breaker, queue limits, node weights, Maintenance schedule. |
| Rolling restart | Restart affected endpoint containers in dependency order. | Image digest, runtime adapter, internal port, secret reference, GPU runtime. |
| Shadow rebuild and promotion | Build and verify a new derived generation before switching. | Embedding identity, dimension, normalization, metric, persistent chunking identity. |
| Backup and migration | Verified backup, short maintenance window, migration, health gate, rollback receipt. | SQLite schema or incompatible canonical-state contract. |

A failed update leaves the previous manifest revision active. In PR 4, `ppctl`
only classifies a future plan; execution is unavailable (`deferred_to_pr5`). A
later authorised operational adapter must not disguise rebuilds or migrations
as live configuration changes.

### PR 4 Deployment Center planning interface

> Status: **current source contract / target runtime deployment**. The typed
> interface and static projection source are present. No listener, host binding,
> endpoint enrollment, or runtime deployment is active or verified here.

The host-only `DeploymentCenter` module exposes exactly two operations. A
configured bridge base is exactly `http://127.0.0.1:<port>/ppctl/v1`; no other
base, operation, or request shape is part of this PR 4 contract:

```text
DeploymentCenter.inspect(installation_ref)
POST <configured bridge base>/inspect
Content-Type: application/json
{"installation_ref":"local-installation"}
  -> platform/resource/catalog/status/model/enrollment/receipt projection

DeploymentCenter.preview(DeploymentPreviewRequest)
POST <configured bridge base>/preview
Content-Type: application/json
{"installation_ref":"local-installation","candidate_manifest":<EndpointManifestV2 JSON>}
  -> recommendation + manifest diff + module/resource estimate
     + hard preflight result + conservative PR 4 update class
     + inspection-only plan hash
```

`installation_ref` is a host-owned identifier, never a browser-supplied path.
The candidate is parsed only as `EndpointManifestV2`; legacy manifest input
that can contain an SSH host is not accepted from the edge. The host may
recommend a profile after read-only inspection; choosing another supported
profile is recorded as a user override, but cannot bypass V2 validity, complete
model-identity checks, immutable artifact evidence, high-risk acknowledgement,
or resource preflight.

The bridge accepts JSON through a bounded streaming reader only: an oversized
declared length is refused before reads, and a chunked body is stopped at the
fixed 128 KiB limit before it can reach `inspect` or `preview`.

`manifest_comparison` is a digest-level summary. When the controller supplies
a safe active-topology projection, `manifest_diff` reports the profile,
module, endpoint, and compute-capability changes without revealing any path,
transport, secret, or raw manifest body; otherwise it explicitly reports that
the diff is unavailable.

`update_class` has a closed vocabulary of `no-change`, `live-apply`,
`rolling-restart`, `shadow-rebuild-promotion`, `backup-migration`,
`enrollment-required`, and `manual-review`. PR 4 only emits `no-change`,
`enrollment-required`, or `manual-review`: its redacted controller projection
never includes an active manifest body or persisted receipt, so it cannot
truthfully classify a changed candidate as a live apply, restart, rebuild, or
migration. Those labels remain target PR 5 operation-adapter outputs. A plan hash binds observed host
state for inspection and drift reporting; it is neither an activation token nor
an authorization to mutate in PR 4. A safe-space failure is a hard refusal, not
an overridable warning.

Enrollment and receipt fields are projections only in PR 4. They distinguish
contract readiness or an unpersisted contract receipt from a server-persisted
receipt, and report unavailable/unverified state honestly. Node contact,
credential transfer, tunnel creation, enrollment consumption, service control,
SQLite mutation, LanceDB promotion, and Maintenance remain deferred.

The existing `http://127.0.0.1:19020/mcp` endpoint is the server/backend MCP
entry. The `http://127.0.0.1:19021` `pp-local-edge` browser entry serves static
content only. When bridge configuration is absent, its no-store configuration
is `disabled` and the browser makes no host request. Only explicit host
configuration may advertise `http://127.0.0.1:<port>/ppctl/v1`; then the
browser directly sends the two fixed JSON `POST` bodies above to `/inspect` or
`/preview`. `pp-local-edge` does not proxy the bridge, mount a host socket, or
receive a Docker/SSH credential. Browser/cache state remains non-authoritative.

The PR 4 collaboration read interface is likewise source-only.
`ProjectWorkingSet.project_for(*, audience: AgentSession, deltas: EventPage)`
requires the working set, audience session, `EventPage.after_cursor`, and next
cursor to share one project and coordination session. The value factory rejects
audience-ineligible events, cursor regression, a non-empty page that does not
advance, and an empty-page cursor gap. Each working-set input class is limited
to 64 items, one page is limited to 20 deltas, and the final projection is
limited to 64 KiB.

That value factory is not an authentication boundary. A trusted feed must bind
the authenticated active `AgentSession`, current policy revision, event schema
and log revisions, projection-factory revision, `cursor_from`/`cursor_to`,
source-page digest, generated-at UTC value, projection digest, and any
independent `AcceptanceReceipt`. Caller-supplied coordinator/reviewer roles or
sessions cannot obtain full-work visibility, and completed work plus artifact
references cannot become accepted artifacts. Objectives, capabilities, event
payloads, prompts, private reasoning, credentials, and result bodies are
redacted; `canonical_memory_effect` is fixed to `none`. PR 5 source and focused
tests now cover durable registry/work-board state, the server binding seam,
authenticated fresh-client Hook continuation, the public bounded work-board
lifecycle, the shadow-to-inject gate, ordinary tool-call reconcile, and bounded
`Stop` progress/submitted event emission. This page therefore makes no claim that the trusted feed is active
or that any live runtime, lifecycle, browser, or production path is verified.

### PR 6 release-readiness contract (target / unverified)

PR 6 keeps release delivery separate from deployment execution. The source
`ArtifactBundle` is immutable build inspection evidence only. The target
Release Bundle contains source revision, package version, protocol/profile/
variant compatibility, image digests, SBOM/provenance references, and an opaque
Model Catalog reference/digest. It does not contain weights, paths, credentials,
canonical state, runtime configuration, or an Execution Grant.

```text
Windows/WSL2: local build/cache + derived-inference GPU smoke only
  -> GitHub protected workflow: target RC/stable evidence + immutable digest
  -> Release Bundle: target selected evidence, not a runtime receipt
  -> server: target verified-digest consumer; sole SQLite writer
  -> stable-only repository: target explicit publication after separate approval
```

The target server may later run Migration Operations, rebuild/promote derived
LanceDB, verify MCP, and transition Maintenance. Those actions require their
own authority and bounded evidence. Neither a documentation field, a digest,
nor a bundle proves that any action is complete.

Workflow Composer is included in PR6 only as an observable `shadow-only`
candidate planner. It must bind frozen plan/compiler/hard-gate/tool-policy
digests and an atomic-skill receipt chain, may not self-attest user-only stages
or remove/reorder mandatory gates, and has no execution or authorization
authority. The fixed route remains the execution authority and deterministic
rollback target. This architecture makes no claim that the shadow comparison
or its adversarial evidence has run.

## 6. Embedding identity and provider routing

The V2 embedding/rerank identity schema is **current**; active provider routing
and any LanceDB Knowledge Generation are **target** runtime behavior. One active
embedding identity will govern one LanceDB Knowledge Generation. Local
and cloud embedding may be fallback peers only when all fields match:

```text
model + fixed revision + artifact/served identity + dimension
+ normalization + distance metric + tokenization/pooling contract
+ golden-vector compatibility evidence
```

Equal dimension alone is insufficient. If identities differ, the active
Deployment Profile chooses one:

- `split-accelerated` and `local-all-in-one` retain the local identity;
- `local-cloud` retains the cloud identity;
- selecting the other identity requires a controlled manifest revision,
  shadow rebuild, validation, and atomic promotion.

For `split-accelerated`, routing is local first. When the node fails and a cloud
provider is configured, enabled, healthy, and identity-compatible, the registered
`pp-compute-node` performs the provider fallback and returns a bounded derived
result; the backend records provider, reason, and revision but never calls the
provider itself.

### Governed index-material migration binding (current source/test)

The migration planner is not allowed to infer a governed model identity from a
legacy server environment. The active control-plane registration supplies the
explicit target identity, and both the read-only plan and mutable apply bind its
SHA-256 digest plus the immutable index-outbox watermark/digest/count snapshot.
This keeps canonical SQLite single-writer migration and derived LanceDB
generation work on the same model identity. `fallback-zero` is rejected on a
governed route.

Project-scoped ordinary-memory correction uses the memory's `project_id` for
every governed embedding probe and retrieval call. The governed embedder also
publishes bounded request/token/cost accounting as health evidence; it does not
expose provider credentials or canonical content. These statements describe
current source and focused-test evidence only. They do not assert a live
migration, generation rebuild, promotion, restart, or production acceptance.

When cloud is missing or also fails:

- canonical writes continue;
- embedding and rerank work enters durable derived-work/outbox state;
- recall uses the current verified generation plus BM25/text/symbolic paths;
- responses expose degradation rather than claiming vector freshness;
- health polling continues for both configured local and cloud providers;
- recovered providers must pass repeated identity and capability probes plus a
  stable window before new work is routed back.

Cloud configuration is sufficient operator authorization for the configured
capability. Plastic Promise does not add content-level data-loss-prevention or
project content filtering. Credential redaction remains mandatory.

## 7. Typed compute capabilities

> Status: the protocol, identity, bounded resource report, manifest-bound
> capability acceptance, admission, lease/fencing, result validation, safe
> receipt schemas, and the isolated compute-node listener are **current source**
> contracts. Verified external runtime placement, transport health, retry,
> reconcile, and production evidence remain **target/unverified**.

The first required capabilities are `embedding`, `rerank`, and fixed-schema
`structured-json`; each has one closed capability version and one closed
body-free result schema. The protocol may later admit capabilities such as
`semantic-chunking`, `memory-classification`, `domain-naming`,
`conflict-analysis`, `knowledge-synthesis`, and `security-review-inference`.

An explicitly bound capability declaration carries a closed `CapabilityBinding`:

- model-identity fingerprint, closed input and result schema identifiers;
- per-capability concurrency plus bounded free-memory and model-cache floors;
- timeout, SHA-256 idempotency-key format, cancellation support, and closed
  terminal-reason semantics;
- input/result-schema-bound, body-free golden-probe input and expected-result
  hashes.

The Deployment Manifest is the authority for this binding. A compute `hello`
and a server requirement can only match it; they cannot introduce a temporary
binding. Admission quarantines a missing or drifting bound capability, an
identity/binding mismatch, or a lower-minor protocol that cannot satisfy the
manifest or requirement. The prior bare `kind`/`contract_version` declaration
remains readable only for backward compatibility and does not supply binding
evidence. A later transport adapter executes the golden probe; this contract
only binds its safe identifiers and hashes.

The interface does not accept arbitrary prompts, tools, file paths, shell
commands, database locations, or MCP administration calls. A host Ollama
runtime is an explicit compatibility adapter, not the production default. The
default compute image manages its own CPU or CUDA runtime and mounts a
controlled model cache without bundling model weights in the image.

## 8. State, data flow, and recovery

Canonical SQLite belongs exclusively to `pp-server-backend`. It stores memory,
governance, active manifest revision, node registration, leases, provider
health, durable work, deployment receipts, and generation selection evidence.
LanceDB remains rebuildable derived state. `pp-local-edge` projects server
state and keeps only bounded temporary session data. `pp-compute-node` keeps
model artifacts, runtime cache, and active lease state, but no canonical data.

Derived work is at-least-once and idempotent, not claimed as distributed
exactly-once. A completion must match both its lease and the server-supplied
current `ComputeFence` for the same derived-work job, so an old lease cannot
commit after a newer generation has been issued. Reconcile recreates missing
eligible work without changing policy.

## 9. Communication and trust

Same-host installations use an internal Docker network. Remote installations
default to outbound, loopback-bound restricted SSH tunnels:

- local edge reaches the server backend through a client SSH tunnel;
- compute creates a reverse SSH tunnel to a server loopback endpoint;
- tunnel identities cannot obtain shell, SFTP, sudo, agent forwarding, or
  unrestricted port forwarding;
- successful transport recovery does not restore scheduling until node,
  model, and golden-probe evidence pass again.

The target Deployment Center guides enrollment. The backend will issue
short-lived, single-use enrollment material for host `ppctl` to transfer and
consume; the frontend never displays or stores long-lived credentials. PR 4
only projects readiness and receipt state—it does not contact a node, transfer
credential material, or consume enrollment.

## 10. Observability and health

`pp-server-backend` is the persistent aggregation point. `pp-compute-node`
reports bounded heartbeat, capability, model identity, load, and latency data.
`pp-local-edge` subscribes to snapshots/events and renders:

- active Deployment Manifest and Release Bundle revisions;
- current provider, fallback reason, health counters, and next probe time;
- queue depth, retries, dead reasons, reconcile lag, and lease expiry;
- Knowledge Generation identity, watermark, readiness, and promotion state;
- tunnel status and last successful identity validation;
- project agent presence, leased work, independently accepted-artifact
  references, blockers, conflicts, and the audience/policy-bound awareness
  cursor without exposing raw prompts or hidden reasoning;
- update plan, affected endpoints, rollback asset, and deployment receipt.

The local edge cache is a display aid only. A Deployment Center refresh must
obtain a new host/server projection; a cached browser result cannot prove disk
space, endpoint enrollment, a plan, or receipt state.

Core MCP health is separate from optional structured inference health. A cloud
structured-JSON failure must not turn a usable MCP with canonical and text
retrieval paths into a generic 503 response.

## 11. Security model

- SQLite is mounted only into `pp-server-backend`.
- The frontend container receives neither Docker socket, host credentials,
  host path access, SQLite access, SSH material, nor arbitrary host execution.
- PR 3 artifact policy excludes databases, LanceDB generations, model weights,
  credentials, private keys, runtime state, logs, and build caches from image
  layers; it grants no live mount by itself.
- CPU/CUDA compute artifacts share the typed result contract. A future compute
  runtime may receive only a controlled read-only model volume and bounded
  scratch space, never canonical-state authority.
- In PR 4, host `ppctl` accepts only typed `inspect` and `preview` planning
  operations and exposes redacted projections; execution is unavailable
  (`deferred_to_pr5`). Future mutation may execute only a separately authorised
  validated plan from a signed catalog or reviewed local release material.
- All listeners are loopback-only or private-container-only by default.
- Manifests, receipts, logs, and metrics exclude API keys, tokens, private keys,
  database contents, user source text, and unrestricted endpoint details.
- Collaboration projections are project- and role-scoped. Delegated agents can
  publish typed events and receipts only within their capability policy; they
  cannot adopt memory, alter another agent's event, merge, deploy, or promote a
  generation.
- Release images are selected by immutable digest; model artifacts are selected
  by fixed revision and hash.
- Incompatible endpoint protocol versions are quarantined by the backend.

## 12. Scale and cost model

The initial target is a personal or small-team installation, so the design uses
three endpoint containers rather than a user-visible microservice fleet.
Concurrency scales first inside `pp-compute-node` through batching and resource
admission. Additional compute nodes can register behind the same typed seam
without changing canonical ownership.

No fixed monthly currency estimate is asserted. Cost is determined by selected
cloud capabilities, request volume, model artifacts, image retention, and
hardware uptime. The frontend must estimate download, expanded image, model,
cache, shadow-generation, and rollback space before apply. Local-first routing,
batching, durable reconciliation, and text degradation bound unnecessary cloud
calls and repeated work.

## 13. Delivery plan and acceptance

Implementation uses six stacked PRs:

1. `routing-core`
2. `endpoint-contracts`
3. `container-artifacts` — source-level plans, descriptors, and artifact policy
   only; no runtime action
4. `deployment-center`
5. `migration-operations`
6. `release-readiness` — target Model Catalog / Release Bundle selection,
   protected immutable evidence, and stable-only handoff; no publication claim

Each PR receives only the smallest deterministic tests for its changed seams,
the bilingual documentation gate, and independent Standards, Spec, and DeepSec
Shield/code-smell receipts bound to the same immutable source, diff,
requirement set, and union-contract revision. After those receipts pass and
merge is explicitly authorized, it may be merged in dependency order and
exercised through a compatibility or shadow deployment slice with an explicit
rollback; the next PR then builds on the merged revision. Broad redundant
suites are not required at every step.

The complete cross-profile, cross-agent, migration, recovery, and rollback E2E
runs after PR 6. It is the final acceptance gate, not the first place basic seam
or schema defects should be discovered: focused per-PR checks must remove those
beforehand. Stable publication and production migration remain separately
authorized operations.

Detailed work and rollback contracts are in
[`implementation-notes.md`](implementation-notes.md). Every PR must also pass
[`documentation-parity.md`](documentation-parity.md); documentation parity is
part of the implementation result, not cleanup after code is complete.
