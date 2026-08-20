# Project Scope and Security Finding Contract

Status: accepted design baseline with local Shield persistence slice (2026-08-05)

This contract fixes the boundary between project isolation, DeepSec evidence,
memory lifecycle, and autonomous promotion. It is deliberately narrower than
the full production rollout: this slice defines the domain state and safety
invariants and now includes the durable Shield queue/finding-version seam plus
the remediation-candidate validation, shadow-canary, proposal-score projection,
and dashboard-read seam. DeepSec execution workers, production orchestration,
canonical-memory promotion, and generation promotion remain outside this
local-only stage.

## Scope authority

- A turn has one immutable `project_id`.
- A session may contain turns from multiple projects; a session is not a
  memory-isolation boundary.
- Missing or conflicting project identity becomes `project:unknown`.
- `project:unknown` may be observed for degraded telemetry only. It cannot
  recall memory, create proposals, write outbox/canonical memory, or enter
  persistent hot cache.
- Explicit `shared`/`global` access requires a reason, evidence relation, and
  governance decision. Similarity alone cannot widen scope.

## Security finding identity

Every persisted finding is bound to:

```text
project_id + commit_sha + scan_revision + rule_id + request_scope_id
```

The identity is a scope boundary, not a memory ID. A finding may later produce
a redacted remediation-pattern candidate, but the candidate never replaces the
finding evidence.

## Finding state axes

Security lifecycle and freshness are independent:

```text
security_state:
open -> remediation_required -> fixed -> resolved
                 |                 |
                 +-> recurring      +-> needs_revalidation

freshness_state:
fresh -> aging -> stale -> expired
```

`false_positive` and `accepted_risk` are governed outcomes, not shortcuts to
`resolved`. Accepted risk has a default 30-day validity and a hard 60-day
maximum, after which it becomes `needs_revalidation`. Both transition-time
creation and direct persistence-boundary reconstruction require a reason and
an explicit expiry plus a UTC start timestamp; records are rejected if the
expiry precedes the start or exceeds the 60-day ceiling.

## Evidence precedence

- The model judges semantic completion and remediation stability.
- Git, DeepSec, tests, review, scope checks, and rollback evidence establish
  engineering facts.
- Safety and scope facts are hard constraints; model output cannot override
  them.
- Incomplete evidence produces `semantically_stable_but_unverified`, which is
  usable only in the originating branch/worktree.
- Judge disagreement produces `judge_conflict` and freezes promotion until new
  evidence arrives.

## DeepSec boundary

- Shield is an asynchronous, required evidence step for formal review.
- Stop/Hook enqueues scans; review waits for the relevant scan revision.
- Shield work reuses the existing `DerivedWorkStore` partition, lease, fencing,
  bounded-retry, and restart-recovery contract under job kind
  `security.shield_scan`; it does not introduce a second queue.
- A claimed batch cannot mix project, provider identity, task type, or scan
  configuration revision.
- Finding output is stored as redacted evidence, never as raw prompt/code
  memory.
- Finding versions and successful job completion commit in one SQLite
  transaction. Project/commit/scan mismatches write nothing, and lineage cannot
  reference a missing or differently scoped parent version.
- Stable, low-risk remediation patterns may become memory candidates only after
  independent project validation and shadow promotion.
- A shadow candidate must pass its local canary before it is linked to the
  existing proposal score projection. The link creates a pending system-origin
  proposal with trusted server provenance; it never adopts memory or writes a
  vector generation.
- A failed canary changes the candidate to `rolled_back` and retains the
  failure reason and bounded metrics. No deletion is used.
- Dashboard projection is read-only and filters candidates by the server-owned
  project scope; secrets in canary evidence are redacted at projection time.
- Spear is a separate, explicitly authorized security route and is never
  triggered by passive memory or ordinary hooks.

## Promotion and rollback

- Branch-local findings and memories do not become project-wide solely because
  a model says “done”.
- The model may mark semantic stability; Coordinator requires matching facts
  before widening scope.
- Revert marks related findings/memories as `superseded` or `rollback-linked`.
- Branch deletion never physically deletes evidence.
- Shared promotion uses project count, successful-use, conflict, freshness, and
  shadow-canary gates. A failed canary rolls back only the affected scope.

## Retention and deletion

- Ordinary governance summaries are retained for 30 days.
- P0/P1 security and isolation evidence is retained for at most 60 days.
- Raw prompts, paths, remotes, secrets, and database contents are not retained.
- Expiry changes visibility/lifecycle only; physical deletion requires a
  separate compliance operation with backup and an irreversible audit summary.

## Implementation order

1. **Local slice complete:** implement the pure project-scoped finding state
   contract and transition tests.
2. **Local persistence seam complete:** add the Shield scan task/outbox store
   with project-scoped idempotency, partitioned batch claim, fencing, bounded
   retries, atomic finding-version completion, and lineage validation.
3. **Local slice complete:** add remediation → rescan → closure evidence;
   `record_rescan` creates a new `resolved` version only with a newer scan
   revision and clean evidence, otherwise records `recurring`.
4. **Local slice complete:** add redacted remediation-pattern candidates,
   require validation in an independent project, and record shadow promotion
   without writing canonical memory.
5. **Local slice complete:** add dashboard projection and local canary recovery
   for the candidate ledger.
6. **Local slice complete:** connect passed shadow candidates to the existing
   proposal score projection; canonical promotion remains a separate,
   evidence-backed operation.
7. Add vector evidence and shadow generation canary metrics to the projection.
8. Only then consider production generation promotion or Spear integration.

No production database migration, worker activation, MCP restart, Maintenance
startup, or generation promotion is authorized or performed by this slice.
