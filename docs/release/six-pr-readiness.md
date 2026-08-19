# Union Six-PR Readiness and Controlled Release Plan

Chinese parity page:
[`six-pr-readiness.zh-CN.md`](six-pr-readiness.zh-CN.md).

> **Status on August 14, 2026:** this is a derived readiness projection of the
> machine-readable [Union Six-PR Contract](../standards/union-six-pr-contract.json),
> revision `2026-08-18.1`. The canonical JSON, not this page, defines scope.
> The [evidence ledger](../standards/union-six-pr-evidence-ledger.json) defines
> evidence state. This page does not assert that any PR, runtime, production
> migration, release, publication, promotion, Maintenance transition, tunnel,
> or MCP restart is complete.

## Integrated completion rule

The six PRs are one dependency line for two mandatory scopes:

```text
PR1 -> PR2 -> PR3 -> PR4 -> PR5 -> PR6
       delivery_scope + collaboration_scope + required_evidence
```

A PR is complete only when **all** of its `delivery_scope`,
`collaboration_scope`, and `required_evidence` items have matching evidence and
all applicable completion gates pass. A delivery-only, collaboration-only,
source-only, test-only, artifact-only, merged, or deployed slice is not whole-PR
completion.

A PR is complete only when its delivery scope, collaboration scope, and
required evidence all pass; one-sided completion is not PR completion.

Evidence remains separated into four classes:

| Evidence class | What it can prove | What it cannot prove by itself |
| --- | --- | --- |
| `implementation` | An immutable source revision contains the stated implementation. | Test success, runtime activation, or production acceptance. |
| `test` | A receipt-bound check passed for that immutable source revision. | A live listener, migration, restart, promotion, or deployment. |
| `runtime` | A specifically identified runtime operation produced a matching receipt. | Production acceptance or public release unless the receipt says so. |
| `production` | A separately authorized production action produced a matching receipt. | Another production action, publication, or release authorization. |

No wording, review opinion, aggregation, branch, tag, path, or artifact
reference may promote one evidence class into another.

## Current union matrix

The summaries below are navigation aids. Exact requirement IDs and statements
remain in the canonical JSON.

| PR | Depends on | `delivery_scope` | `collaboration_scope` | `required_evidence` and gates | Readiness claim |
| --- | --- | --- | --- | --- | --- |
| **PR1 — Routing core and collaboration foundation** | None | 4 items: one typed query/index/rerank route, exact embedding identity and bounded recovery, durable derived-work queue/reconcile, and mandatory project isolation/quarantine for every task path. | 6 items: immutable collaboration values, append-only event-log foundation, ProjectWorkBoard boundary, least-privilege policy contract, three-plane separation, and server-authenticated independent `AcceptanceReceipt`. | 6 evidence items. Source/revision/document/asset/Standards/Spec/DeepSec/evidence-ledger gates apply. | Governed only by ledger receipts; no runtime or production completion is asserted here. |
| **PR2 — Endpoint contracts and shared lease semantics** | PR1 | 3 items: closed three-endpoint authority, server-owned role/action profiles, and typed protocol/model/resource/terminal identities without runtime persistence. | 4 items: shared Lease/Fence/Heartbeat/ResultReceipt/Retry/Reconcile semantics for Agent work and compute jobs while their records, policies, capabilities, bodies, and authorization planes remain distinct; compute receives no collaboration or canonical authority. | 5 evidence items, including the same conformance suite against both adapters and cross-plane negative proofs. All common review/drift/evidence gates apply. | Source contracts or fake adapters alone cannot complete PR2. |
| **PR3 — Container artifacts and Passive Memory events** | PR2 | 4 items: secret-free artifact compiler and role/platform/variant matrix; immutable OCI/SBOM/provenance/rootfs/role evidence; server-only collaboration packaging; no runtime, push, migration, promotion, Maintenance, restart, or production mutation. | 4 items: bounded Stop events, accepted-result events only after independent server acceptance, semantic capture stays pending-only, promoter input is receipt/evidence/conflict bound, and Hook payloads reject prompts, hidden reasoning, credentials, result bodies, lease tokens, and provider secrets. | 5 evidence items, including artifact isolation, Hook behavior, promotion-negative tests, no-runtime assertion, and the three independent reviews. | Build policy or a completed result plus artifact references is not accepted work and is not runtime evidence. |
| **PR4 — Deployment Center and collaboration retrieval** | PR3 | 3 items: host-only `inspect`/`preview`, bounded/redacted/profile-aware planning, and a non-authoritative local edge with fresh host/server projections. | 5 items: bounded Project Working Set and awareness projection, separate memory/peer ranking, role-aware relevance, a server-authenticated audience/policy/source/cursor/digest tuple, and independent `AcceptanceReceipt` lineage for accepted artifacts. | 5 evidence items, including forged-role/audience/receipt/source/cursor negatives and `context_supply` separation. All common gates apply. | Caller-supplied coordinator/reviewer roles, sessions, pages, or result strings grant no visibility or acceptance authority. PR4 remains incomplete until trusted feed and consumer evidence exists. |
| **PR5 — Migration operations and collaboration runtime** | PR4 | 8 items: durable server-owned migration orchestration and typed phase adapters; canonical SQLite/LanceDB/retention boundaries; strict server-versus-compute provider separation; hot `local`/`cloud`/`hybrid` routing; compute-only credentials with atomic profile activation and identity revalidation; bounded structured JSON; and operation-specific fail-closed degradation. | 10 items: persistent AgentRegistry/ProjectWorkBoard and authenticated session lifecycle; Hook/MCP/stage/closure events; Maintenance reconcile; shadow-to-inject awareness and frontend projections; pending-only accepted-result promotion; shared-but-distinct compute work semantics; bounded secret-free dispatch; and project-scoped hot mode transitions that preserve in-flight work. | 9 evidence items covering migration/recovery, persistent collaboration lifecycle, Hook/MCP E2E, promotion negatives, frontend smoke, three independent reviews, endpoint-role denial, routing/credential/profile activation, and stable degradation/recovery. | Source and focused-test slices do not prove a live persistent runtime, production migration, provider activation, or publication. |
| **PR6 — Release readiness and role-capability contracts** | PR5 | 4 items: cross-platform installers and profiles, immutable RC/release bundle, Windows/WSL2 local build/cache/GPU-smoke boundary plus protected GitHub evidence and verified-digest server consumption, and upgrade/rollback/recovery/retention/UTC behavior. | 4 items: OCI authority labels, final server/edge/compute isolation, cross-Agent E2E, and Workflow Composer as observable `shadow-only` behavior with deterministic fixed-route rollback and no execution/authorization authority. | 7 evidence items. The Workflow Composer adversarial gate is added to all common gates; RC, internal deployment, production migration, public publication, stable publication, and release-repository synchronization each require separate authorization receipts. | An RC contract, artifact bundle, role label, or candidate workflow is not runtime E2E, production acceptance, or stable publication. |

### Current PR5 evidence boundary

The current source tree contains implementation and focused-test slices for the
server-owned collaboration runtime, compute-node-only embedding/rerank/structured
JSON execution, hot `local`/`cloud`/`hybrid` routing, compute-only credential
projection and profile activation, bounded degradation, and the existing
migration-operation contracts. The evidence ledger still controls completion:
every PR5 requirement remains `not-evidenced` until a governed same-class receipt
binds the final immutable source revision and exact changed-path set. This page
does not bind a source commit or assert that the independent reviews, live
browser/runtime lifecycle, production migration, provider activation,
publication, or production acceptance have completed. Source or focused-test
results cannot populate runtime or production evidence, so the PR5 integrated
completion rule remains unsatisfied.

## Completion gates that apply to every PR

- `U6-GATE-SOURCE-01`: every receipt binds an immutable source revision and the
  relevant diff or artifact digest.
- `U6-GATE-REVISION-01`: generated views, ledgers, manifests, and review
  receipts bind revision `2026-08-18.1` and the raw canonical-source SHA-256;
  later revisions must prove immutable lineage from the previous source.
- `U6-GATE-DOCUMENT-01` and `U6-GATE-ASSET-01`: affected English/Chinese docs,
  diagrams, SVGs, badges, links, resource tables, and pricing tables remain in
  semantic parity with no blocking tracked drift.
- `U6-GATE-REVIEW-01` and `U6-GATE-DEEPSEC-01`: independent Standards, Spec,
  and DeepSec Shield/code-smell receipts bind the same immutable source, diff,
  requirement set, and contract revision. DeepSec is read-only and its findings
  never become canonical memory automatically.
- `U6-GATE-EVIDENCE-01`: all requirement IDs have explicit implementation,
  test, runtime, and production states; cross-class promotion is forbidden.
- `U6-GATE-COMPOSER-01`: PR6 additionally proves that Workflow Composer remains
  shadow-only, cannot remove hard gates or self-attest user-only stages, and
  deterministically falls back to the fixed route.

## Non-negotiable authority boundaries

- `pp-server-backend` / `pp-core` is the sole canonical SQLite writer and the
  only coordination, governance, accepted-result, receipt-persistence, and
  LanceDB-promotion decision authority.
- LanceDB is rebuildable derived state and never a recovery authority.
- Edge submits bounded intent/events and reads bounded projections only.
- Compute executes bounded derived inference only; matching `project_id`, model
  dimension, role, capability, manifest, or result shape never grants authority.
- Coordination, Project Working Set, and Canonical Memory remain separate.
  Peer progress, agreement, findings, semantic capture, or submitted work never
  becomes canonical memory automatically.
- Persisted times and lease/fence comparisons use timezone-aware UTC. Installers
  do not modify Linux, macOS, Windows, WSL2, edge, server, or compute host
  timezone settings.

## PR6 target release authority pipeline

```text
Windows / WSL2
  -> local build, cache, GPU smoke, and derived inference only
  -> no canonical write or release authority

Protected GitHub workflow
  -> immutable RC/stable build evidence, OCI digest, SBOM, provenance

Server
  -> pulls and runs only a verified digest
  -> owns MCP, SQLite, LanceDB promotion decision, and Maintenance

Stable release channels
  -> PyPI, GHCR, GitHub Release, and release-repository sync
  -> each requires its own explicit authorization receipt
```

This is a target authority model, not evidence that any environment is
configured, healthy, deployed, migrated, promoted, restarted, or published.

## Controlled handoff and rollback

1. Verify each PR against the exact canonical requirement set and its evidence
   ledger entries; do not infer whole-PR status from a source slice.
2. Bind Standards, Spec, and DeepSec review receipts to the same immutable
   source revision, diff digest, requirement set, and contract revision.
3. After PR6 evidence is complete, evaluate an immutable RC candidate. RC
   creation remains separately authorized.
4. Internal deployment, production migration, public publication, stable
   publication, and release-repository synchronization remain distinct actions
   with distinct authorization and receipts.
5. On failure, retain canonical SQLite, select a previously verified immutable
   bundle/digest, and rebuild LanceDB only from canonical state.

See [Release Delivery and Installation Profiles](delivery.md) and the
[three-endpoint architecture](../architecture/three-endpoint-deployment/architecture.md)
for derived operational detail.
