# Union Six-PR Documentation Parity Standard

Chinese parity page:
[`documentation-parity.zh-CN.md`](documentation-parity.zh-CN.md).

> **Normative binding:** this standard is a derived projection of the
> machine-readable [Union Six-PR Contract](../../standards/union-six-pr-contract.json),
> revision `2026-08-18.1`, raw-source SHA-256
> `2c7e4a532e17cde229830479712aabe3ca36a13e21fafbe9fcf781cd91305722`.
> The canonical JSON wins on disagreement. The
> [derived-document manifest](../../standards/union-six-pr-derived-documents.json)
> defines the tracked document/asset family, and the
> [evidence ledger](../../standards/union-six-pr-evidence-ledger.json) defines
> evidence state.

## Integrated rule

Documentation parity is a blocking gate on every PR, not a cleanup task left
to PR6. A PR is complete only when every `delivery_scope`,
`collaboration_scope`, and `required_evidence` item passes. Documentation that
covers only deployment or only collaboration is incomplete even when its two
languages agree.

A PR is complete only when its delivery scope, collaboration scope, and
required evidence all pass; one-sided completion is not PR completion.

Documentation evidence proves only the documentation/test layer named by its
receipt. It cannot prove a listener, persistent runtime, migration, restart,
promotion, Maintenance transition, RC, stable publication, or production
acceptance.

## Single-source policy

1. Normative scope changes edit the canonical JSON, increment its revision,
   preserve immutable previous-revision lineage, regenerate the bilingual
   canonical views, and update governed projections in one change set.
2. Exact PR requirement text is not re-authored in README, roadmap,
   architecture, TODO, or release pages. Those pages link the canonical source
   and summarize it without narrowing, replacing, or reordering it.
3. Status is read from typed evidence receipts. Words such as implemented,
   tested, running, deployed, promoted, released, or complete must name the
   evidence class they actually prove.
4. Manual prose claiming `pass` is not a parity receipt. A receipt must bind
   the immutable source revision, diff digest, contract revision/hash,
   requirement set, changed-file set, checks, and UTC timestamp.
5. Historical local receipts are provenance only. They do not carry forward to
   a new source revision, diff, requirement set, or union-contract revision.

## Tracked inventory

The derived-document manifest is the inventory authority. Each affected PR
must evaluate every applicable family, including:

- English and Chinese Markdown pairs;
- Mermaid and ASCII architecture diagrams;
- English and Chinese SVG pairs;
- badges and their target links;
- internal relative links and public canonical links;
- commands, paths, ports, environment names, profile names, image names, model
  identities, schema versions, and defaults;
- pricing, download size, disk, memory, GPU, concurrency, retention, and other
  resource tables.

`enforcement: tracked-drift` is a blocking failure record, not a waiver or a
passing state. Drift is resolved only when the underlying documents/assets are
repaired and the manifest is regenerated or amended from verified evidence.

## Required semantic parity

For every affected English/Chinese pair:

- topics and navigation expose equivalent behavior and authority boundaries;
- defaults, failure modes, rollback, degradation, and non-goals agree;
- commands, identifiers, paths, ports, profiles, model identities, schema
  versions, units, dates, and resource assumptions agree unless an explicit
  platform difference is recorded;
- architecture diagrams show the same ownership and data flow;
- SVG pairs use the same topology, revision, facts, badge/link set, and status;
- removed features or flags disappear from both languages;
- target, experimental, source-only, runtime, production, and released states
  use equivalent labels;
- the canonical contract link and integrated completion rule are present when
  required by the manifest;
- source/test claims are never worded as runtime/production claims.

## Collaboration and acceptance claims

Documents that describe Project Working Set, awareness, or accepted artifacts
must preserve these boundaries from revision `2026-08-18.1`:

- `project_for(*, audience: AgentSession, deltas: EventPage)` is a
  non-authoritative value factory, not caller authentication;
- caller-provided coordinator/reviewer roles or constructed sessions do not
  grant full-work visibility;
- a trusted feed binds a server-authenticated active session, current policy,
  source kind/authority, event schema/log/factory revisions,
  `cursor_from`/`cursor_to`, source-page/projection digests, generated-at UTC,
  and independent `AcceptanceReceipt` lineage;
- `completed + artifact_refs`, a reviewer string, or an unbound
  `ResultReceipt` is not accepted work;
- peer progress, agreement, findings, semantic capture, and submitted work do
  not become canonical memory automatically.

### PR 5 source/test status (2026-08-17)

The bilingual architecture family must describe authenticated fresh-client Hook
continuation, the bounded public `ProjectWorkBoard` lifecycle, Dashboard Agent
topology/work-board/event-timeline projections, and Maintenance collaboration
composition/lifecycle as **current source implementation / focused tests
passed / live runtime and production evidence pending**.

The same documents must present the server-owned `WorkReceipt` issuer,
accepted-result-to-pending-only orchestration, ordinary tool-call reconcile,
and bounded `Stop` progress/submitted emission as **current source implementation
/ focused tests passed / live runtime evidence pending**. Real browser smoke, authenticated runtime/lifecycle E2E,
live Maintenance transition, deployment/activation, and production evidence
remain unverified. Documentation parity does not promote source/test evidence
into runtime or production evidence.

The deployment, remote-control, architecture, Mermaid, and ASCII families must
also agree that generation preparation and cutover are separate. Preparation
builds, reconciles, and verifies an inactive candidate. Cutover requires an
independently authorized stopped runtime; Control activation and retarget use
authenticated CAS APIs; restart, health/retrieval smoke, and Maintenance
transition remain separate host operations. No document may describe direct
Control SQLite repair, an embedded restart flag, or automatic Maintenance
enablement as part of the operator cutover tool.

## Review requirements

Every formal PR needs three independent review channels bound to the same
immutable source revision, diff digest, requirement set, and contract revision:

1. Standards conformance;
2. Spec conformance;
3. DeepSec Shield and code-smell review.

DeepSec is restricted to repository/diff/web reads and read-only MCP. It has no
shell, file, database, release, or production write authority, and its findings
never become canonical memory automatically.

## Machine-readable receipt contract

The authoritative parity result belongs in a generated machine-readable
receipt. English and Chinese human views may render that same receipt but must
not maintain independent counters or conclusions.

```json
{
  "schema": "plastic-promise/documentation-parity-receipt/v1",
  "contract": {
    "path": "docs/standards/union-six-pr-contract.json",
    "revision": "2026-08-18.1",
    "sha256": "2c7e4a532e17cde229830479712aabe3ca36a13e21fafbe9fcf781cd91305722"
  },
  "source_revision": "<immutable source revision>",
  "diff_sha256": "<sha256>",
  "requirement_ids": ["<affected PRn-Dxx/PRn-Cxx/PRn-Exx>"],
  "changed_files": ["<repository-relative path>"],
  "document_families": ["<manifest id>"],
  "checks": {
    "bilingual_markdown": "pass|fail",
    "diagrams_and_svg": "pass|fail",
    "badges_and_links": "pass|fail",
    "resource_and_pricing_tables": "pass|fail",
    "status_and_evidence_classes": "pass|fail",
    "canonical_contract_binding": "pass|fail"
  },
  "intentional_differences": [],
  "result": "pass|fail",
  "generated_at_utc": "<timezone-aware UTC timestamp>"
}
```

A missing receipt, mutable source identity, stale contract revision/hash,
mismatched file set, independent language counters, unexplained difference, or
any failed check keeps the gate not-evidenced or failed.

## Focused local checks

Use repository-native verifiers first. The minimum focused checks for this
document family are:

```bash
python scripts/render_union_six_pr_contract.py
python scripts/verify_union_six_pr_contract.py --repo-root .
python -m pytest -q -o addopts='' tests/test_union_six_pr_contract.py
git diff --check
```

When a previous canonical source is available, the verifier must also receive
`--previous-contract <immutable-path>` so current-artifact self-consistency
cannot hide an unchanged revision over changed bytes. Asset, link, resource,
pricing, or render checks are added only when the changed family requires them.

## Gate outcome

This standard does not self-issue a `pass`. The current outcome is read from
the derived-document manifest and evidence ledger after the checks above create
receipts for the exact immutable source. No Markdown edit alone completes a PR
or proves runtime/production state.
