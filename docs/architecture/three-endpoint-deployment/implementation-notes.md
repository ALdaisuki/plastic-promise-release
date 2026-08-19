# Three-Endpoint Deployment Implementation Notes

> This is an implementation contract, not evidence that the target runtime is
> deployed. Status date: 2026-08-14. Current PR 5 collaboration capabilities are
> implemented in source and their focused tests passed; live runtime and
> production evidence remain pending.

> **Normative scope:** the generated union-contract documents and their source
> [`union-six-pr-contract.json`](../../standards/union-six-pr-contract.json),
> revision `2026-08-18.1`, define completion. The PR headings below organise
> implementation work; they cannot remove or defer responsibilities assigned
> to the same PR by the union contract.

Only when every `delivery_scope`, `collaboration_scope`, and
`required_evidence` item passes is a PR complete; one-sided completion is not
PR completion, and source/test evidence is not runtime/production evidence.

Chinese parity document:
[`implementation-notes.zh-CN.md`](implementation-notes.zh-CN.md).

## Status legend

| Label | Meaning in this plan |
|---|---|
| **current** | Source-level code/contract exists in its stacked PR worktree; it is not deployment evidence. |
| **legacy-current** | Existing repository/runtime path remains the compatibility baseline, not a host-health claim. |
| **target** | Deferred work in a named later PR. |
| **unverified** | Live external state was not proven; source contracts alone are not runtime evidence. |

## Delivery discipline

The work is a six-PR dependency line. A completed stage may be committed,
pushed, and opened. It may be merged in PR 1 through PR 6 order only after its
focused evidence and three independent Standards, Spec, and DeepSec
Shield/code-smell review receipts pass against the same immutable source,
diff, requirement set, and union-contract revision **and the user explicitly
authorizes that PR's merge in the current task**. The next PR builds on the
merged revision rather than keeping six unmerged layers alive.

Each PR must:

- use the latest `main` containing the preceding PR's merged or squash-merge
  revision as its base; PR 1 also starts from the latest `main`, never from a
  still-open predecessor feature branch;
- contain its own upgrade, degradation, and rollback statement;
- update both English and Chinese documentation in the same commit set;
- pass the documentation parity gate in
  [`documentation-parity.md`](documentation-parity.md);
- add focused contract tests at changed seams rather than a broad redundant
  test sweep;
- exercise the merged slice through a reversible compatibility or shadow
  deployment when that PR changes runtime behavior;
- preserve SQLite as canonical truth and LanceDB as rebuildable derived state;
- avoid production activation, stable publication, or automatic/unreviewed
  merge.

The complete cross-profile, cross-agent, migration, recovery, and rollback E2E
runs after PR 6. It is the acceptance gate, not the primary bug-discovery loop:
small deterministic checks and quick review must remove basic schema, scope,
authority, and rollback defects before the final flow is started.

Across the final stack, endpoint containers, generated native runtime assets,
and browser timestamp rendering use logical UTC. This changes no Linux, macOS,
or Windows host timezone; persisted timestamps and lease/fencing comparisons
remain timezone-aware UTC.

## PR 1: `routing-core`

### Goal

Make governed inference routing a deep module used by query embedding, index
embedding, and rerank. Optional provider failure must not make the canonical MCP
runtime generically unhealthy.

PR 1 establishes the retrieval-routing seam and uses it for live query probes,
principle semantic activation, and the existing LanceDB/embedder integration.
The current PR5 runtime keeps hosted and local inference behind the registered
`pp-compute-node` seam. If no eligible node exists, the server returns a stable
defer/original-order degradation; it does not rediscover a provider locally.
Typed rerank routing, provider lifecycle recovery, and full cross-provider
compatibility evidence remain separately gated evidence.
The same PR also establishes the minimum project-collaboration foundation so
parallel agents cannot share or claim unscoped work while the remaining
coordination capabilities are added later.

### Required changes

- Introduce one routing interface that accepts a typed capability request and
  returns a result or stable degradation reason.
- Place local-node, cloud, and deterministic text-degradation adapters behind
  that seam; only the compute-node endpoint may construct an inference
  adapter.
- Enforce exact embedding identity compatibility for fallback.
- Apply profile selection when local and cloud identities differ.
- Add durable queue/reconcile behavior for unavailable derived work.
- Add immutable, non-secret collaboration value contracts for project scope,
  agent identity, coordination and agent-session snapshots, work/result
  receipts, typed events, audiences, and cursors. These values do not prove
  registration, grant authority, or implement a mutable lease lifecycle.
- Add a project/session-scoped, append-only `CollaborationEventLog` adapter
  foundation with idempotent event identity, causal-parent scope checks,
  bounded cursor reads, role/agent audience filtering, expiry, and an explicit
  injected SQLite connection or path. PR 1 does not wire it to MCP, Hooks, or a
  production authentication boundary.
- Migrate existing task rows and every existing enqueue, dedupe, claim,
  heartbeat, complete, review, inbox, release, and recovery path to require
  `project_id`; ambiguous legacy rows enter a non-claimable quarantine until
  reassigned by server governance. This hardens the existing task lifecycle;
  it is not a new `ProjectWorkBoard` service.
- Keep developer-agent collaboration values and compute jobs as separate typed
  records. Persistent `AgentRegistry`/`ProjectWorkBoard` adapters and their new
  mutable lease/session lifecycle remain PR 5 target work.
- Define the three-plane invariant: coordination events are not Project
  Working Set truth, and neither is canonical memory; peer progress is never
  automatically promotion-eligible.
- Specify bounded health polling, circuit state, consecutive-success recovery,
  and a stable window before automatic local restoration for PR 2's endpoint
  contract and lifecycle implementation.
- Separate core MCP readiness from optional structured inference readiness.

### Focused evidence

- one query-embedding route contract test;
- one index/outbox route contract test;
- one rerank route contract test;
- PR 1 query identity and project-scope routing tests; later PR 2 tests cover
  mismatch/profile selection and stable recovery;
- health projection test proving optional cloud failure is not a core 503;
- one immutable-contract rejection matrix for secret/private-reasoning fields;
- one append/idempotency/causal-scope/audience/cursor event-log test slice; and
- one cross-project negative matrix for the existing task lifecycle plus its
  additive schema/recovery transaction.

### Documentation gate

Audit and synchronize `README.md`, `docs/README.zh-CN.md`, all existing English
and Chinese architecture SVGs, deployment/profile pages, pricing/resource
tables, GitHub badges, and repository links. This specification does not edit
those existing files; PR 1 must enumerate and correct drift before it can be
declared complete.

### Rollback

Deactivate the new routing policy, keep durable work and audit evidence, and
return callers to deterministic defer/original-order behavior. Do not delete queued work,
project-scoped rows, append-only collaboration events, or canonical memory.
PR 1 has no awareness injection, persistent Agent registry, or collaboration
memory promotion to disable.

## PR 2: `endpoint-contracts`

### Goal

Define the three endpoint modules at pure-contract level through one deep,
source-only `EndpointAuthority` Module. Its `resolve` -> `assess` ->
`verify_completion` Interface compiles and enforces a closed role/action
profile while OS/runtime enforcement remains intentionally deferred.

### Current source-level scope

- Define versioned interfaces for `pp-local-edge`, `pp-server-backend`, and
  `pp-compute-node`.
- Add the pure `EndpointAuthority` deep Module. Its small Interface is
  `resolve`, `assess`, and `verify_completion`; `EndpointContractRegistry`
  remains a compatibility name where retained by source callers.
- Compile an `EndpointAuthorityProfile` on the server from a closed role/action
  matrix. `project_id`, manifest/hello claims, and advertised capability strings
  are validation inputs and never authority grants.
- Limit `pp-local-edge` to intent submission and bounded read projections. It
  cannot write canonical state or acquire collaboration, governance,
  compute-persistence, or deployment authority.
- Resolve canonical SQLite ownership, LanceDB-promotion-decision ownership, and
  receipt-persistence ownership to `pp-server-backend`; also make it the sole
  canonical-state, inference-job scheduling/validation, collaboration, and
  governance writer/decision owner. Provider execution belongs only to
  `pp-compute-node`; no mount or write is performed by this pure contract.
- Define Deployment Manifest v2, Resolved Deployment Plan, endpoint protocol
  versions, and compatibility errors.
- Define typed `embedding` and `rerank` schemas with closed capability/result
  versions, identity evidence, and an optional backward-readable
  `CapabilityBinding`. A bound capability carries model-identity fingerprint,
  input/result schema identifiers, resource floors, concurrency, lease timeout,
  SHA-256 idempotency format, cancellation/terminal-reason semantics, and
  body-free golden-probe hashes. The manifest is authoritative; hello and
  requirement must compare with it rather than supply an ad-hoc binding.
  Fixed-schema `structured-json` is now a compute-node capability. The backend
  schedules a registered node and validates the bounded result; it does not
  invoke a hosted provider or assemble inference context.
- Define leases, server-supplied current fences, heartbeats, bounded resource
  reports, and completion checks that enforce a bound capability's schema,
  fingerprint, timeout, and terminal-reason rules when a binding is present.
- Preserve the PR 1 developer-Agent collaboration values as a separate plane
  from inference compute jobs, even when both carry the same `project_id`. A
  compute endpoint may lease bounded inference work and return only derived
  `embedding`/`rerank` results plus health, resource, model, and timing evidence.
  Fixed-schema structured-semantic inference is covered by the current
  `structured-json` compute-node capability and remains subject to its
  identity and bounded-payload gates.
  It cannot obtain Task Queue, `AgentRegistry`, work-board,
  collaboration-event-writer, awareness, memory/knowledge-promotion, merge,
  deployment, Maintenance, or LanceDB-promotion authority.
- Define server-owned `ManifestRevisionRecord` and `DeploymentReceipt` schemas;
  pure decisions may produce receipt values, but PR 2 does not persist records.

### Upgrade and degradation

- **Upgrade:** this source-only change lets adapters resolve a closed authority
  profile, validate and re-parse a secret-free effective V2 manifest, compare a
  bound capability across
  manifest/hello/requirement, quarantine lower-minor protocol and binding drift,
  and reject a completion whose lease is no longer the server's current fence
  or bound capability contract.
- **Degradation:** PR 2 does not activate routing, listeners, schedulers, or
  persistence. Existing runtime paths remain **legacy-current**; callers that
  opt into these pure validators receive only stable rejection reasons and no
  fallback execution is initiated by the contract.

### Explicitly deferred to PR 3–PR 6

- Container images, Compose, volume/mount enforcement, endpoint listeners,
  private transport, `ppctl`, and Deployment Center.
- Manifest/receipt persistence, actual durable-job scheduling/lease issuance,
  result storage, retry/reconcile side effects, and runtime health proof.
- Runtime activation of authority profiles, transport adapters, or persistence
  adapters; PR 2 only returns pure decisions and values.
- Production SQLite migration, LanceDB shadow rebuild/promotion, Maintenance,
  MCP restart, RC/stable release, and production acceptance.

### Focused evidence

- schema parse/round-trip and secret rejection tests, including bound
  capability identity/schema/resource/lease/golden-probe facts;
- resolved server-only ownership assertions for SQLite and LanceDB promotion,
  explicitly without container/mount claims;
- closed role/action-profile assertions proving claims and capability strings
  cannot grant authority, plus local-edge and compute denial cases;
- one cross-plane check proving matching `project_id` does not turn an inference
  lease into developer-Agent collaboration work;
- protocol compatibility and quarantine tests, including lower-minor mismatch;
- lease expiry, binding timeout/schema/terminal-reason mismatch, result/lease
  mismatch, and stale-current-fence rejection tests.

### Rollback

Revert the source-only endpoint-authority contract commit as one review unit.
PR 2 does not activate a runtime, persist records, or require a migration; any future
backward-readability and verified-backup rollback obligation belongs to PR 5.

## PR 3: `container-artifacts`

### Goal

Implement the build-time policy and inspection boundary for three independent
endpoint artifacts without introducing a user-visible microservice fleet or
activating any endpoint runtime.

### Required changes

- Add `ContainerArtifactCompiler.prepare(request) -> ArtifactBuildPlan` and
  `materialize(plan, executor) -> ArtifactBundle` as a secret-free source seam.
- Produce the role × platform × variant policy matrix for `pp-local-edge`,
  `pp-server-backend`, compute CPU, and compute CUDA; CUDA is limited to its
  supported platform policy and both compute variants expose only
  `embedding/v1` / `rerank/v1`.
- Require immutable OCI, SBOM, and provenance evidence from the narrow
  `ArtifactBuildExecutor` adapter; a fake executor is valid focused evidence.
- Declare non-root, read-only-rootfs, listener, layer-exclusion, and logical
  mount policies. `pp-server-backend` alone is eligible for canonical-state
  read-write; compute gets only read-only model-catalog plus bounded runtime
  scratch; edge has only bounded ephemeral status-projection cache.
- Add/align source recipes for the three roles and both compute variants;
  recipe presence remains source evidence, not a completed build result.
- Package the existing PR 1 collaboration contracts and event-log foundation
  only in the server artifact's application surface. Edge may receive bounded
  read-projection contracts; compute receives no collaboration authority.
- Prove through role packaging/SBOM inspection that edge and compute cannot
  import or configure the collaboration event writer. PR 3 does not add
  persistent `AgentRegistry`/`ProjectWorkBoard` adapters, wire a listener, or
  activate the event log in a runtime.
- Keep Model Catalog reference/digest opaque, keep model weights outside image
  layers, and treat host Ollama as an explicit compatibility adapter.
- Do **not** locally activate Docker/Compose, bind listeners, allocate runtime
  GPU, create tunnel assets, use release credentials, deploy, migrate SQLite,
  promote LanceDB, restart MCP, or make a production change. A protected CI
  adapter may perform no-push OCI build verification only; it does not start a
  container, run GPU inference, or create a runtime/deployment receipt.

### Focused evidence

- deterministic request/matrix/rejection tests and a fake-executor immutable
  evidence/inspection-receipt check;
- entrypoint, listener, mount, authority, non-root/read-only-rootfs, and
  capability-policy inspection;
- source-recipe and layer-exclusion checks for models, databases, credentials,
  runtime state, logs, and build caches;
- artifact inspection proving the PR 1 event-writer foundation is server-only
  and edge/compute cannot import or configure it; and
- an explicit assertion that PR 3 never locally activates Docker/Compose or
  invokes a listener, tunnel, migration, promotion, Maintenance, MCP restart,
  or production endpoint; protected CI verification is limited to no-push OCI
  build evidence.

### Rollback

Revert the source-level artifact policy and recipes as one review unit. PR 3
does not mutate a runtime, image registry, model volume, canonical state, or
deployment receipt, so it has no operational image rollback step. PR 5 owns
any verified runtime rollback after a separately authorized migration.

## PR 4: `deployment-center`

### Goal

Use `pp-local-edge` only as the static browser entry at
`http://127.0.0.1:19021`, with host `ppctl` as the planning adapter. The
existing `http://127.0.0.1:19020/mcp` endpoint remains the server/backend MCP
entry. Its planning contract is current in source, while runtime deployment
remains target-only: no listener, host binding, or runtime deployment is active
or verified here. PR 4 has no execution surface: execution is unavailable
(`deferred_to_pr5`). As of 2026-08-11, `ProjectWorkingSet` and
`AgentAwarenessProjection` are also current immutable source contracts; their
server feed and every persistence/runtime binding remain deferred to PR 5.

### Current source-level collaboration projection (2026-08-14)

- `ProjectWorkingSet` is an immutable, rebuildable, non-authoritative project
  snapshot. It accepts only same-project/session `AgentSession`, `WorkReceipt`,
  bounded candidate `ResultReceipt`, `blocker.raised`, and `conflict.detected`
  values. Each input class is capped at 64 items; duplicates, cross-scope values,
  future timestamps, and wrong event/result types fail closed. A completed
  result plus artifact references is not accepted work; accepted-artifact
  projection requires the independent server-authenticated `AcceptanceReceipt`
  from PR1-C06.
- `AgentAwarenessProjection` is an audience-specific read view. One delta page
  is capped at 20 events and the complete canonical JSON at 64 KiB. Project/
  session scope, audience visibility, cursor regression, non-advancing pages,
  and empty-page cursor gaps all fail closed.
- `project_for(*, audience: AgentSession, deltas: EventPage)` is a value factory,
  not an authentication boundary. A role string or caller-created
  coordinator/reviewer session grants no visibility. Full-work consumption
  requires a server-authenticated active session, current policy claim,
  event-log source lineage, audience-bound `EventPage.after_cursor`, projection
  digest, and independent `AcceptanceReceipt` lineage. Other authorized roles
  receive only owned work, dependencies of owned work, and work referenced by
  events visible to that audience.
- The projection retains the project-level `goal_summary` but omits concrete
  `WorkReceipt.objective`, `AgentIdentity.capabilities`, and
  `CollaborationEvent.payload`. Raw prompts, hidden reasoning, credentials, and
  result bodies do not enter the view.
- Both contracts declare `canonical_memory_effect: none`. The working set is not
  a second truth store, and its projection cannot grant lease, review,
  acceptance, execution, or memory authority. The trusted feed must reject
  source substitution, cursor gaps/regression, stale policy or factory
  revisions, forged pages/digests, self-issued receipts, and cross-scope data.
  Persistence, `AgentRegistry`, `ProjectWorkBoard`, Hook/MCP, and runtime binding
  remain deferred to PR 5.

### Required changes

- Add one host-only `DeploymentCenter` deep module with exactly
  `inspect(installation_ref)` and `preview(DeploymentPreviewRequest)`. The
  only configured HTTP bodies are `POST <base>/inspect` with
  `{"installation_ref":"local-installation"}` and `POST <base>/preview` with
  `{"installation_ref":"local-installation","candidate_manifest":<EndpointManifestV2 JSON>}`.
- Make `ppctl` a closed typed dispatcher for only `inspect` and `preview`;
  reject `apply`, enrollment consumption, shell, Docker, SSH, path, and generic
  command requests.
- Accept JSON through a bounded streaming reader only: reject an oversized
  declared length before reads and stop a chunked body at the fixed 128 KiB
  limit, before either operation can dispatch.
- Keep the optional edge-to-host bridge disabled when configuration is absent.
  Only explicit host configuration may expose
  `http://127.0.0.1:<port>/ppctl/v1`; then the browser directly sends the two
  fixed JSON `POST` bodies above. The no-store bridge configuration says
  `disabled` by default; `pp-local-edge` never proxies it or mounts a host
  socket.
- Add environment/resource inspection for macOS, Linux, and Windows/WSL2;
  recommend a Deployment Profile while recording a supported user override.
- Render module, expanded-image, model, cache, shadow, and rollback estimates;
  fail closed when the host preflight projects free space below its safe
  threshold.
- Render enrollment readiness, endpoint status, complete model-identity
  comparison, V2 manifest diff, and an inspection-only update classification.
  PR 4 may emit only `no-change`, `enrollment-required`, or `manual-review`
  because its controller projection may provide only a redacted active topology,
  never an active manifest body or persisted receipt; later action classes
  require the PR 5 authorized adapter. Also render an
  inspection-only plan hash and receipt projection, distinguishing an
  unpersisted contract receipt from a server-persisted receipt.
- Accept candidate input only as secret-free `EndpointManifestV2`; resolve
  host paths, legacy node records, and local evidence only inside the host
  adapter. The edge never receives raw paths, credentials, Docker access, SSH
  material, SQLite access, or arbitrary host execution.
- Keep the local-edge view/cache explicitly non-authoritative. A fresh host or
  server projection is required for each status/plan claim.
- Keep the current bounded `ProjectWorkingSet` and role-aware
  `AgentAwarenessProjection` source contracts. Any edge/runtime consumer may
  receive the same redacted projection only after PR 5 binds an authenticated
  active `AgentSession`, current policy claim, event schema/log/factory
  revisions, audience-bound cursor range, source-page/projection digests, and
  independent `AcceptanceReceipt` lineage. It cannot read the event log, raw
  prompts, hidden reasoning, credentials, result bodies, or unrestricted
  history directly.
- Keep this as a read-only source/projection contract. Persistent registry/work
  state, `AgentRegistry`, `ProjectWorkBoard`, and authenticated server-feed,
  MCP/Hook, and runtime binding remain PR 5 work.
- Keep collaboration retrieval separate from canonical-memory relevance. A
  peer delta may change the current work plan, but it cannot override a user
  instruction or be represented as an adopted project fact.
- Do not contact a node, transfer/consume enrollment material, create a tunnel,
  create service assets, start/stop a service, mutate SQLite, promote LanceDB,
  enable Maintenance, or persist a deployment receipt.

### Focused evidence

- two-operation interface and fixed-allowlist tests;
- V2-only candidate, host-path/credential rejection, and response-redaction
  tests;
- profile recommendation/override, module/resource estimate, model-identity,
  manifest-diff, safe PR 4 update-class, and hard safe-space-refusal tests;
- static browser-asset checks for bridge states and the non-authoritative
  projection; PR 5 runtime-adapter smoke coverage owns its full update classes;
- cursor resume, role visibility, project-isolation, bounded response, and
  non-authoritative working-set projection tests, including the 64-item source,
  20-event delta, and 64 KiB output boundaries plus objective/capability/payload
  redaction; negative cases must cover caller-created coordinator/reviewer
  sessions, cross-audience cursor reuse, forged source/page/projection digests,
  stale policy/factory revisions, self-issued acceptance, and
  completed-plus-artifact without an `AcceptanceReceipt`;
- explicit assertions that PR 4 has no apply, node contact, credential
  transfer, tunnel, service, database, promotion, Maintenance, or receipt
  persistence action.

### Rollback

Retain the active server manifest and restore the previous local-edge image if
the planning UI has separately been activated. PR 4 has no production mutation
to roll back. The backend remains manageable through the narrow host adapter
and does not lose canonical state when the frontend is unavailable.

## PR 5: `migration-operations`

### Goal

Provide a short-maintenance migration from the current systemd runtime to the
three-endpoint containers, with verified rollback.

### Authority and source status

The PR 5 typed orchestrator and durable journal **source contract is current**
in this worktree; live phase-adapter composition and runtime activation remain
**target**.
This document is not evidence of a live listener, container, tunnel, migration,
promotion, or restart. One server-owned `MigrationOperation` orchestrator
coordinates the transition through typed adapters. The browser
Deployment Center and host `ppctl` remain **current read-only** planning surfaces:
they must not accept an apply command, open canonical SQLite, consume enrollment
material, or persist a migration receipt. In production composition,
`pp-core`/`pp-server-backend` remains the sole canonical SQLite writer and the
sole owner of the durable migration lease. `SQLiteMigrationExecutionJournal`
provides the cross-process grant/lease/fence/receipt CAS; the in-memory adapter
is for tests and explicitly non-production composition only.

The operation is intentionally split into two non-interchangeable records:

- a short-lived, secret-free **Migration Operation Plan** that binds the source
  and target artifact digests, runtime/node/derived-index observations,
  canonical-state fingerprint, backup and rollback capacity, and a drift fence;
- an explicit, operation-bound **Execution Grant** that matches a fresh plan and
  is issued only after the server's admission/risk policy is satisfied. A
  Deployment Center inspection `plan_hash` is never an Execution Grant.

Mutable `apply` rejects digest-only transport projections and requires the
server-memory topology and artifact bindings checked at plan creation. The
journal rejects concurrent/replayed operations, consumes the grant once at the
first mutable boundary, and persists one-shot state, monotonic fencing, and the
secret-free terminal receipt. Expired running work is marked
`recovery-required` and cannot be silently replayed.

The typed adapter seam is limited to fixed phases (preflight, backup,
rehearsal, enrollment/tunnel and capability checks, cutover, shadow rebuild and
promotion, Maintenance transition, rollback, and receipt persistence). Adapters
receive typed inputs and stable reason codes only; they never receive arbitrary
shell, Docker, SSH, or SQLite commands. The intended contracts cover preflight
and drift rejection, verified online backup/integrity evidence, rollback asset
selection, shadow-generation quality gates, Maintenance enablement, and the
five-day production-backup/daily-temporary-cache retention policy. The source
contract is exercised with typed/fake adapters only; no production phase has
been verified successfully.

### Current source slice and remaining runtime work

- **Current source:** expose the server-owned typed `MigrationOperations.plan`,
  `preflight`, and `apply` seam plus runtime, node, canonical-state, and
  derived-index adapter protocols.
- **Current source:** create and validate a separate Migration Operation Plan,
  validate a typed Execution Grant, and reject stale plans, changed
  observations, unavailable nodes, replayed grants/operations, expired leases,
  and stale fence completion.
- **Current source:** install the durable journal tables only through the
  backup-gated versioned deployment migration; runtime construction only checks
  schema presence and never mutates it implicitly.
- **Current source:** provide server-only durable collaboration schema/stores for
  Agent/session/role/plan/work/lease/activity/event/cursor/result/acceptance
  state, server-issued formal-result submitter assignment, typed stage events,
  replay/idempotency/fencing checks, reconcile logic, and promotion validation
  with an outbox. These are source/test slices, not runtime or production
  completion.
- **Current source / focused tests passed:** authenticated fresh-client Hook
  continuation preserves the `session-init` authentication lineage and covers
  `agent.closed`, lease release, cursor resume, and the shadow-to-inject gate.
  Real authenticated lifecycle E2E remains unverified.
- **Current source / focused tests passed:** bounded public
  list/claim/heartbeat/review/accept `ProjectWorkBoard` entrypoints and read-only
  Dashboard Agent topology/work-board/event-timeline projections are present.
  Registration now derives a server-owned `WorkReceipt` from the exact
  authenticated session; real browser/runtime smoke remains unverified.
- **Current source / focused tests passed:** Maintenance collaboration
  composition and lifecycle wiring are present. The live production
  Maintenance transition remains unverified.
- **Current source / focused tests passed:** ordinary authenticated tool calls
  reconcile presence, every exact-session active lease, and the incremental
  feed under one canonical writer transaction.
- **Current source / focused tests passed:** `Stop` emits bounded
  `work.progressed` events for live leased work or `work.submitted` events only
  when a canonical server-persisted result exists; prompts and assistant bodies
  are never copied into the collaboration log.
- **Current source / focused tests passed:** accepted work atomically enqueues a
  receipt/evidence/conflict-bound pending-only promotion job. Adoption remains
  a separate governed action.
- **Target runtime:** pre-pull and verify immutable image digests before the
  maintenance window.
- **Target runtime:** create and integrity-check an online backup, then rehearse
  migration on the backup copy.
- **Target runtime:** start local edge and compute first; verify enrollment,
  tunnel, capability, and embedding identity.
- **Runtime evidence pending:** exercise the bounded work-board, authenticated
  Hook continuation, awareness gate, Dashboard, and Maintenance collaboration
  lifecycle through real browser/runtime E2E. Local edge must consume only
  bounded awareness deltas.
- **Runtime evidence pending:** exercise server-owned work issuance, ordinary
  tool reconcile, bounded Stop progress/submitted events, and accepted-result
  pending-only promotion through the authenticated live lifecycle. Progress,
  heartbeats, assumptions, raw prompts, and peer agreement alone remain
  ineligible; proposal adoption remains a separate governed action.
- **Target runtime:** stop old MCP/Maintenance, take the final backup, migrate,
  and mount canonical SQLite only into `pp-server-backend`/`pp-core`.
- **Target runtime:** build and verify a shadow LanceDB generation before atomic
  promotion, then enable Maintenance by default after successful cutover.
- **Target runtime:** retain production backups for at most five days and clear
  temporary cache daily, without treating LanceDB as recovery authority.
- **Target runtime:** persist a secret-free Migration Receipt containing ordered
  phase results, rollback state, safe evidence hashes, and stable failure reasons.

- **Current source / focused tests passed:** Dashboard Agent topology, project
  work board, and collaboration event timeline include project/session/role
  filtering, cursor refresh, and empty/error/stale states. Real frontend/browser
  smoke and runtime lifecycle evidence remain pending.

The current source keeps the receipt-shaped phase result in memory only; a
server persistence adapter is still **target** and must not be inferred from
the source tests.

### Focused evidence

- typed orchestrator/adapter contract tests for plan/grant binding, phase order,
  drift fences, and stable refusal reasons;
- backup/integrity/migration/restore rehearsal on isolated state;
- single-writer lock and duplicate-runtime refusal test;
- tunnel loss/recovery, outbox replay, and generation promotion/rollback smoke;
- service restart and active-manifest recovery smoke;
- persistent agent/work restart recovery, stale-lease reconcile, MCP/Hook scope
  binding, shadow awareness, conflict visibility, accepted-result-to-pending-
  proposal, and peer-progress non-promotion smoke.

All of the above are source or isolated-test evidence until an independently
authorised production adapter is wired. At the time of this documentation
update, no live listener, container, tunnel, production migration, LanceDB
promotion, Maintenance transition, or MCP restart has been verified.

### Rollback

Stop new containers, restore the verified pre-cutover backup and prior
generation selection, then start the old systemd runtime. Retain the failed
deployment receipt and reason for audit.

## PR 6: `release-readiness`

### Goal

Make installation and coordinated upgrades usable without weakening protected
release authority.

### Target scope and non-claims

PR 6 is a **target/unverified** release-readiness contract, not a completed
release. Its source-level Model Catalog is opaque metadata evidence: fixed
model identity/revision and compatibility/resource metadata, without weights,
paths, tokens, node addresses, or a signature/publication claim. Its Release
Bundle is a target selection/evidence record that binds source/package
compatibility, a profile/variant matrix, immutable artifact references, and a
Model Catalog ref/digest. An Artifact Bundle remains build-inspection evidence;
it is not release authority, a deployment grant, a migration proof, or a server
receipt.

- **Target:** provide quick deployment entry points for macOS, Linux, and
  Windows/WSL2, plus `local-all-in-one`, `local-cloud`, and
  `split-accelerated` manifests.
- **Target:** describe basic/recommended hardware configurations and update
  classes, without asserting a successful install or upgrade.
- **Target:** have Windows/WSL2 perform local build/cache/GPU smoke for derived
  inference only; it never becomes a SQLite writer or release authority.
- **Target:** have a protected GitHub workflow create immutable OCI/SBOM/
  provenance evidence and a selected Release Bundle; it is not claimed to have
  run, signed, published an RC, or released stable artifacts here.
- **Target:** have the server consume a verified digest as runtime only, retain
  `pp-server-backend`/`pp-core` as the single canonical SQLite writer, and keep
  LanceDB rebuildable derived state. No deployment, migration, promotion, MCP
  restart, or Maintenance transition is asserted.
- **Target:** keep public PyPI, GHCR stable, GitHub Release, and release-
  repository sync behind separate explicit approval after internal acceptance.
- **Target:** bind role-capability evidence for collaboration: server owns
  registry/work/event/promotion authority, edge owns bounded awareness display,
  and compute owns none of those capabilities.
- **Target:** include Workflow Composer only as a `shadow-only`, observable,
  non-authoritative candidate planner. Bind frozen plan/compiler/hard-gate/tool-
  policy digests and the atomic-skill receipt chain; the fixed route remains
  execution authority and deterministic rollback target.
- **Target:** after all six PRs are merged, run the complete cross-agent E2E:
  join, project-scoped claim, peer delta, review/conflict, accepted receipt,
  pending proposal, governed non/acceptance, restart recovery, and rollback.

### Focused evidence

- source/contract tests for catalog and bundle identity, compatibility, and
  secret-free validation;
- documentation-only validation of the Windows/WSL2 -> protected GitHub ->
  verified-digest server -> stable-only authority split;
- final bilingual documentation parity report and diagram checks;
- role-capability/SBOM checks and the final cross-agent acceptance receipt,
  including a negative proof that peer progress never becomes canonical memory;
- Workflow Composer adversarial evidence rejecting stale completion, execution-
  revision mutation, user-only self-attestation, tool escalation, hard-gate
  removal/reordering, receipt-chain gaps, source/contract mismatch, execution
  authority, and fixed-route rollback failure.

### Rollback

If a later target release gate fails, select a previously reviewed immutable
Release Bundle/digest and rebuild derived LanceDB only from canonical state.
Stable publication and any server mutation remain untouched unless separately
authorized.

## Final stack audit and merge

For each PR from 1 through 6:

1. Rebase or update it onto the latest `main` containing the preceding merged
   or squash-merge revision, without flattening its review boundary or using
   the predecessor feature branch as the long-lived base.
2. Run only its deterministic seam tests and bilingual documentation gate.
3. Obtain independent Standards, Spec, and DeepSec Shield/code-smell receipts
   bound to the same immutable source revision, diff digest, requirement set,
   and union-contract revision; resolve every blocking finding. DeepSec remains
   read-only and its findings do not enter canonical memory automatically.
4. When green, request and receive explicit user authorization for that PR's
   merge. Only then merge it, run its reversible compatibility/shadow deployment
   slice, and verify rollback before starting the next PR.

After PR 6 is merged:

5. Run the complete cross-profile and cross-agent E2E plus migration/recovery/
   rollback rehearsal; use it for final acceptance, not broad exploratory
   debugging.
6. Verify documentation parity, diagram references, badges, resource figures,
   links, and role-capability evidence across English and Chinese files.
7. Create an RC and perform internal deployment acceptance.
8. Request separate authorization for production migration or public stable
   publication.

## Non-goals for this stack

- Kubernetes, service mesh, or a public inference listener.
- Multiple canonical SQLite writers, database replication, or dual-write cutover.
- Arbitrary agent prompts or tool execution on compute nodes.
- Content-level cloud data interception or DLP policy.
- Automatic merge, public release, or production mutation from an open PR.
