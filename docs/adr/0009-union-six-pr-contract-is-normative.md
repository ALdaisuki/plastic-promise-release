---
status: accepted
date: 2026-08-11
---

# ADR 0009: The union six-PR contract is normative

Chinese parity page:
[`0009-union-six-pr-contract-is-normative.zh-CN.md`](0009-union-six-pr-contract-is-normative.zh-CN.md).

## Canonical binding

- Source: [`docs/standards/union-six-pr-contract.json`](../standards/union-six-pr-contract.json)
- Schema: `plastic-promise/union-six-pr-contract/v1`
- Revision: `2026-08-18.1`
- Raw-source SHA-256:
  `bc7b90b55bb2c14c5ff12a9c8b73448bf3e8142a23b777d95719d4e1a1c99f90`
- Previous revision: `2026-08-11.1`, bound by the canonical source's
  `revision_lineage`

The canonical JSON wins over this ADR, generated Markdown, roadmaps,
architecture pages, TODO lists, PR descriptions, commits, tests, receipts,
deployed artifacts, and historical conversations whenever they disagree.

## Context

The composable-deployment plan and project-scoped multi-Agent collaboration
plan were previously described in separate documents and conversations. That
allowed a deployment-only PR allocation to survive after collaboration duties,
acceptance security, DeepSec review, evidence classes, and Workflow Composer
governance had been added. It also made source-level work easy to overstate as
whole-PR, runtime, or production completion.

A stable decision needs one machine-readable scope, explicit change control,
typed evidence, and generated/verified projections.

## Decision

Plastic Promise adopts the canonical JSON above as the only normative contract
for the PR1-to-PR6 dependency line.

A PR is complete only when its delivery scope, collaboration scope, and
required evidence all pass; one-sided completion is not PR completion.

1. Deployment and collaboration are two mandatory scopes inside the same six
   PRs, not parallel roadmaps or optional add-ons.
2. A PR is complete only when every `delivery_scope`, `collaboration_scope`,
   and `required_evidence` item passes every applicable completion gate.
3. Implementation, test, runtime, and production are distinct evidence
   classes. Evidence from one class never satisfies another class by wording,
   aggregation, reviewer opinion, merge state, or artifact reference.
4. Coordination, Project Working Set, and Canonical Memory remain separate.
   Peer progress, agreement, findings, semantic capture, and submitted work do
   not become canonical memory automatically.
5. `AcceptanceReceipt` is a server-authenticated, immutable, project/session-
   scoped decision with independent submitter and reviewer sessions plus
   WorkReceipt/ResultReceipt/evidence digest, policy revision, conflict state,
   source revision, and UTC issuance binding. `completed + artifact_refs`, a
   reviewer string, or self-attestation is insufficient.
6. Caller-provided role, capability, project ID, manifest, model identity,
   audience, session, cursor page, or result shape is validation input, never
   an authority grant. Trusted collaboration projection requires a server-
   authenticated session/policy/source/cursor/digest binding.
7. Every PR requires independent Standards, Spec, and DeepSec Shield/code-smell
   review receipts bound to the same immutable source revision, diff digest,
   requirement set, and union-contract revision. DeepSec remains read-only and
   its findings never enter canonical memory automatically.
8. Workflow Composer belongs to PR6 only as `shadow-only`, observable,
   non-authoritative behavior. The fixed route remains execution authority and
   rollback target.
9. Bilingual documents, diagrams, SVGs, badges, links, resource tables, and
   pricing tables are governed projections and must remain synchronized.

## Governance artifacts

- The canonical bilingual views are generated from the same JSON bytes:
  [`union-six-pr-contract.md`](../standards/union-six-pr-contract.md) and
  [`union-six-pr-contract.zh-CN.md`](../standards/union-six-pr-contract.zh-CN.md).
- The [evidence ledger](../standards/union-six-pr-evidence-ledger.json) records
  an explicit implementation/test/runtime/production state for every
  requirement ID.
- The
  [derived-document manifest](../standards/union-six-pr-derived-documents.json)
  enumerates critical bilingual document and asset families. A
  `tracked-drift` entry is blocking evidence, not a waiver.
- Generated views, ledgers, manifests, and governed review receipts bind the
  exact contract revision and raw-source digest.

## Change procedure

A normative amendment must, in one reviewed change set:

1. edit the canonical JSON;
2. increment its revision;
3. preserve immutable lineage to the previous canonical source and digest;
4. regenerate the bilingual canonical views and governed projections;
5. update the evidence ledger and derived-document manifest when required;
6. pass the contract, revision, document, asset, review, DeepSec, and evidence
   gates.

Changing generated Markdown, a roadmap, an ADR, a PR description, or a status
report alone cannot amend normative scope. One-time execution authorization
may permit a bounded action but does not rewrite the contract or imply another
authorization.

## Consequences

- Status reports must name the exact evidence class they prove.
- A delivery-only or collaboration-only slice cannot be called a completed PR.
- Source and test work cannot be described as a live listener, persistent
  runtime, migration, restart, LanceDB promotion, Maintenance transition, RC,
  stable release, or production acceptance without matching receipts.
- Derived documents may summarize the contract, but they must link it and may
  not silently narrow, replace, or reorder its requirements.
- Revision or digest drift fails closed; current-artifact self-consistency is
  not proof of monotonic revision history.

## Non-decision

Accepting this ADR does not assert that any implementation, test, runtime,
production, release, publication, migration, promotion, restart, or
Maintenance action has completed. Those states remain entirely receipt-driven.

## Supersession

This ADR may be superseded only by a later bilingual ADR that identifies the
replacement canonical source and revision and is itself adopted through the
canonical amendment procedure. A generalized future Delivery Program Contract
may provide a reusable execution module, but it cannot silently alter this
historical union-six-PR instance.
