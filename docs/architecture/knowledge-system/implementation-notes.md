# Plastic Promise Knowledge System Implementation Notes

Status: implementation guide; review-scan and evolution-evidence seams landed, remaining phases pending

## 1. Delivery Strategy

Build one vertical slice at a time and keep each feature behind explicit gates. The first production slice is:

```text
upload Markdown
  -> immutable Source Version
  -> structure-v1 Evidence Chunks
  -> lexical knowledge_search with citations
  -> Dashboard source/job view
```

Cloud semantic analysis, automatic domains, Wiki artifacts, knowledge LanceDB, Hook injection, and physical purge follow only after the canonical slice passes backup and restore tests.

### Slice 2a status (landed on codex/knowledge-semantic-foundation)

- Schema: `knowledge_semantic_jobs`, `knowledge_semantic_units`, `knowledge_domains`,
  `knowledge_claims`, `knowledge_artifacts`, `knowledge_citations`.
- `knowledge/semantic.py`: batch planner (project/space/version + adjacency, default 20),
  untrusted-response validator (grounding, enums, caps, secret shape), durable coordinator
  with leases, retry/backoff, and reconcile; provider seam degrades to `None` when
  chunk-inference env is unconfigured.
- `knowledge/domains.py`: candidate registry, activation thresholds, merge/split/retire
  with reversible lineage.
- `knowledge/artifacts.py`: Wiki lifecycle draft -> validated -> active | pending_review,
  citation-coverage gate, high-risk curator review.
- CLI read-only: `knowledge semantic-status`, `knowledge domains`, `knowledge artifacts`.

Cloud JSON compilation and knowledge LanceDB vectors still require operator-configured
chunk-inference and embedding credentials; the deterministic slice is fully testable
without them.

### Slice 1 scope: Markdown-first

The first production slice accepts Markdown/text sources only (`markdown-text-v1` parser, `structure-v1` evidence chunks). PDF, Office, HTML, and other binary formats are deliberately deferred to later milestones and must not be added to the ingestion adapter set before the Markdown slice passes backup/restore and retrieval smoke tests.

Suggested top-level source area after implementation begins:

```text
plastic_promise/knowledge/
  contracts.py
  ingestion.py
  query.py
  governance.py
  models.py
  repository.py
  jobs.py
  domains.py
  claims.py
  artifacts.py
  promotion.py
  metrics.py
  adapters/
    blob_filesystem.py
    source_upload.py
    source_folder.py
    source_git.py
    source_url.py
    parser_markdown.py
    parser_html.py
    parser_pdf.py
    parser_office.py
    inference_gateway.py
```

Keep the external module interfaces in `contracts.py`; callers and tests should not import internal pipeline stages.

## 2. Phase 1 - Canonical Foundation

### 2.1 Paths and ownership

Proposed server state:

```text
/srv/plastic-promise/state/knowledge/
  plastic_knowledge.db
  blobs/
  quarantine/
  backups/
  generations/
  knowledge.env
```

All directories and files are owned by `plastic:plastic`. Database and EnvironmentFile modes are 0600; directories containing private material are 0700. Never place a production database or source blob inside a Git checkout.

### 2.2 Schema and migration

Create a dedicated schema owner with transactional, idempotent migrations. Migration commands support `--check`, `--dry-run`, and explicit backup evidence. Do not reuse the memory database's schema-version table.

First tables:

- `knowledge_spaces`
- `knowledge_sources`
- `knowledge_source_versions`
- `knowledge_chunks`
- `knowledge_ingest_jobs`
- `knowledge_audit_events`
- `knowledge_index_outbox`
- `knowledge_generations`

Add domains, semantic units, claims, artifacts, citations, and purge jobs in later migrations when their vertical slices are implemented.

### 2.3 BlobStore

Use SHA-256 content addressing and atomic `temp -> fsync -> rename`. The database commit references only an admitted Blob. Deduplicate identical bytes without merging Source identities. Tests use a temporary filesystem adapter.

### 2.4 Backup and restore

- SQLite Online Backup API for `plastic_knowledge.db`.
- Blob manifest containing path, size, and hash.
- Generation manifest copied as evidence; LanceDB data itself remains rebuildable.
- Restore test into an isolated directory, then run SQLite integrity checks, Blob hash verification, and lexical query smoke.

## 3. Phase 2 - Deterministic Ingestion

### 3.1 HTTP upload

Run a separate knowledge-ingestion process on `127.0.0.1:9050`. Implement:

- `POST /v1/uploads` for resumable quarantine upload;
- `POST /v1/sources` for source registration and idempotent submission;
- `GET /v1/jobs/{job_id}` for project-scoped status;
- `GET /v1/events` for SSE progress;
- read endpoints for Dashboard sources, versions, and chunk projections.

The browser cannot choose arbitrary server paths. `server_path` sources must resolve under admin-configured roots.

### 3.2 Parser adapters

Start with Markdown, text, HTML, and text-bearing PDF. Add DOCX, PPTX, XLSX, CSV, and JSON through structured parsers that return a common `NormalizedDocument` contract. Preserve page/sheet/slide/table/heading metadata and parser identity.

Do not execute macros, scripts, formulas, embedded programs, or archives. Treat extracted links as data.

### 3.3 Evidence chunks

Reuse `plastic_promise.core.chunking` rather than creating a second structure parser. Extend the normalized input contract to carry media anchors while keeping `structure-v1` the sole owner of text spans. Add a new schema revision only if the existing manifest cannot represent page, sheet, slide, or time anchors.

## 4. Phase 3 - Semantic Compilation

### 4.1 Batch contract

Batch only chunks sharing project, Knowledge Space, Source Version, and adjacent structure. Default to 20 chunks and flush partial batches after 30 seconds.

Semantic planning and provider execution run in the isolated `127.0.0.1:9050`
ingestion process, never in the MCP request path. The worker scans active
projects and Evidence Chunks with rotating keyset cursors so configured page
limits bound each cycle without permanently starving older projects or chunks.
Provider calls are capped at four concurrent requests. Durable jobs retain
unique-owner leases, heartbeat renewal, retry, failure-code, and reconcile
state in SQLite. A worker must renew its lease before canonical projection
writes, derived promotion, and completion; stale owners are fenced out.
Derived promotion happens before the semantic job becomes `done`, so promotion
failure returns the same durable job to retry with `semantic_promotion_error`.

The ingestion health response includes only the semantic gate, running state,
last bounded result, last cycle timestamp, and sanitized exception type. It
must not include source text, provider responses, credentials, or exception
messages.

Worker controls:

| Environment variable | Default | Bound | Purpose |
|---|---:|---:|---|
| `PP_KNOWLEDGE_SEMANTIC_INTERVAL_SECONDS` | `15` | `1..3600` | Idle polling interval; successful ingestion wakes the worker immediately. |
| `PP_KNOWLEDGE_SEMANTIC_PROJECT_LIMIT` | `100` | `1..1000` | Active-project page size per cycle; pages rotate instead of truncating the project set. |
| `PP_KNOWLEDGE_SEMANTIC_PLAN_CHUNK_LIMIT` | `500` | `1..10000` | Evidence Chunk planning page size per project and cycle. |
| `PP_KNOWLEDGE_SEMANTIC_BATCH_LIMIT` | `5` | `1..100` | Maximum durable semantic jobs claimed per cycle. |
| `PP_KNOWLEDGE_SEMANTIC_PARTIAL_FLUSH_SECONDS` | `30` | `0..300` | Maximum age before a partial source-version batch becomes eligible. |

The cloud response is JSON with:

- variable-count Semantic Units;
- referenced Evidence Chunk IDs for every unit;
- candidate primary/secondary Knowledge Domains;
- entities and typed links;
- Claims with temporal scope and evidence stance;
- contradiction candidates;
- Wiki update recommendations.

Reject unknown chunk IDs, cross-project IDs, unsupported claims, malformed enums, oversized fields, or source text not present in the cited chunks.

### 4.2 Automatic domains

Use a separate `knowledge_domains` registry. Model-created domains start as candidates but may auto-activate when evidence, distinct usage, and separation thresholds pass. Scheduled maintenance:

- merges near-duplicates while preserving aliases;
- splits domains with multiple stable semantic communities;
- retires unused domains after policy thresholds;
- never changes project access or moves content across Knowledge Spaces;
- records reversible lineage for every rename, merge, split, or retirement.

Do not reuse `DomainManager.PREDEFINED_DOMAINS`; those are behavior domains with principle semantics.

### 4.3 Claims and conflicts

Persist supports/refutes/qualifies evidence edges. A newer source does not delete an older Claim. Low-risk current-best resolution may be automatic; high-risk conflict resolution requires a Curator. Query projections display unresolved disagreement.

### 4.4 Wiki artifacts

Generate source summaries first, then entity, concept, topic, overview, and saved-query artifacts. Every paragraph or bounded claim group must retain citations. Markdown is an export/operator projection; SQLite lifecycle and citation rows remain canonical.

## 5. Phase 4 - Retrieval and MCP

### 5.1 Lexical first

Ship SQLite lexical retrieval and source hydration before vector search. It provides a deterministic degraded path and validates project/lifecycle/citation gates without a cloud index.

### 5.2 Knowledge LanceDB

Build a dedicated shadow generation from active Semantic Units and Knowledge Artifacts. Store only derived vectors and bounded search projections. The canonical Source Version, Evidence Chunk, Claim, and citation rows remain in SQLite.

The implemented gate is `PP_KNOWLEDGE_LANCE_SHADOW`, with `off` as the default. The `KnowledgeLanceShadowBuilder` is the deterministic writer seam; worker/CLI/outbox scheduling is intentionally deferred to the next retrieval slice. Values `shadow` and `on` both run the same rebuildable shadow writer when called explicitly: neither value activates a generation, changes query routing, promotes to canary/production, or invokes Maintenance. Maintenance scheduling and promotion remain deliberately unwired.

The current canonical scope is the complete project. `all` is the stored domain identity and `knowledge` is a normalized alias. Any domain-specific identifier fails with `knowledge_lance_domain_bindings_unavailable` before provider resolution or generation writes; narrower builds require canonical `knowledge_domain_bindings` first.

Generation identity includes:

- provider/base URL identity without secret material;
- embedding model and dimension;
- every active project's Source Version revision, including active sources that emit no vector row;
- persisted chunking schema, algorithm, budgets, limits, and offset convention;
- semantic schema/prompt hashes;
- active source revision-set hash;
- index projection and fusion policy.

The builder reads one transactionally consistent canonical snapshot, writes only the derived generation, then reloads the canonical snapshot immediately before the `shadow` transition. A changed source revision set, corpus projection, or chunking identity records a sanitized `knowledge_lance_canonical_changed` failure instead. Each generation status transition and its bounded `knowledge_audit_events` row commit in one SQLite transaction.

### 5.3 MCP tools

Add tools through existing server manifest and routing patterns:

- `knowledge_search`
- `knowledge_explain`
- `knowledge_source_submit`
- `knowledge_job_status`
- `knowledge_domain_list`

Binary upload remains HTTP because MCP JSON/base64 upload would inflate context and couple long transfers to the query server. High-risk review, domain mutation, tombstone, and purge actions use authenticated Dashboard/admin contracts and defense checks rather than `alwaysAllow` tools.

### 5.4 Context fusion

Extend `context_supply` internally with a Knowledge Query adapter. Preserve existing core/related/divergent layers, but add `item_type`, `knowledge_kind`, lifecycle, domain, Source Version, and citation projections. `memory_recall` must not begin returning knowledge records.

## 6. Phase 5 - Hook and Dashboard

### 6.1 Hook

Extend the current user-level Codex Hook adapter rather than adding another hook process.

Before prompt:

1. resolve project and Knowledge Space;
2. run local intent routing;
3. read the project-session hot cache;
4. render a bounded `<knowledge-routing>` block;
5. enqueue prefetch on miss;
6. return before 400 ms or fail open with a degradation event.

Stop:

- retain existing memory proposal behavior;
- attach only knowledge citations actually used in the turn;
- never ingest a full transcript as a Source;
- never call `auto_context_inject` a second time.

### 6.2 Dashboard V2

Add unframed project-scoped routes within the current Dashboard:

- Knowledge Sources
- Upload/Connect Source
- Ingestion Jobs
- Knowledge Domains
- Wiki Artifacts
- Claims and Conflicts
- Generation Status
- Retention and Purge

The first read-only semantic slice uses the existing Dashboard V2 response
envelope and exposes:

- `GET /api/dashboard/v2/knowledge-semantic` for project-scoped durable job counts;
- `GET /api/dashboard/v2/knowledge-domains` for a bounded 100-row domain projection;
- `GET /api/dashboard/v2/knowledge-artifacts` for a bounded 100-row Wiki artifact projection.

These routes never mutate knowledge state. They project only the authorized
`project_id`, identify SQLite as authority, and treat domains, artifacts, and
future vector indexes as derived navigation. The initial domain/artifact views
are intentionally bounded single pages; they must not render cursor controls
until the backend implements matching keyset pagination.

Use dense operational tables, status filters, progress bars, source-type icons, explicit tooltips, and stable responsive dimensions. Do not create a marketing page or a second dashboard.

The browser talks directly to the Mac-forwarded 19050 endpoint. Enforce Origin, Bearer token, role, project ACL, JSON/content type, and idempotency on every write.

## 7. Phase 6 - Governance and Promotion

### 7.1 Artifact promotion

```text
draft -> validated -> active | pending_review
pending_review -> active | rejected
active -> stale | contested
```

Low-risk artifacts auto-promote only when citation coverage, schema, project, source validity, and contradiction gates pass. `finance`, `medical`, `legal`, `security`, and `production_operations` artifacts require Curator approval.

### 7.2 Generation promotion

```text
building -> shadow -> canary -> production -> retired
```

Use immutable evidence and CAS activation. Do not infer generation readiness from vector dimension or environment variables. Required evidence includes provider smoke, full rebuild, outbox reconciliation, fixed bilingual quality results, forbidden-hit zero, stale leakage zero, restart recovery, and restore/rebuild rehearsal.

### 7.3 Physical purge

Automatic maintenance may advance eligible Sources through tombstone and purge after 30 days. The purge executor must resolve exact database IDs and Blob hashes, create backup evidence, recalculate dependent lifecycle state, and never accept a caller filesystem path.

## 8. Testing Strategy

### 8.1 Interface tests

Test through the deep module interfaces:

- submit identical bytes to the same Source and prove Source Version reuse;
- submit identical bytes as two distinct Sources and prove two Source Versions share one Blob without merging Source identity;
- expire a worker lease and prove safe recovery;
- update one source and prove only affected derived objects change;
- query active/stale/contested combinations and prove lifecycle gates;
- test in-memory adapters and temporary SQLite through the same interface contracts.

### 8.2 Security tests

- path traversal, symlink escape, MIME mismatch, zip bomb, oversized body;
- URL SSRF, redirect escape, DNS rebinding protections, unapproved schemes;
- cross-project Source/Chunk/Claim IDs;
- browser actor/trust spoofing;
- stale ETag, idempotency conflict, role downgrade, expired token;
- secret-shaped content in logs and error bodies.

### 8.3 Retrieval tests

- bilingual source recall@1/@5 and MRR;
- citation precision and coverage;
- exact identifier retrieval through lexical fallback;
- vector/model outage degradation;
- conflicting claims and temporal supersession;
- forbidden cross-project hit and stale/contested leakage, both exactly zero.

### 8.4 End-to-end smoke

```text
upload -> job complete -> source/version/chunks visible
       -> knowledge_search -> exact citation
       -> context_supply -> typed memory + knowledge context
       -> Hook cache hit/miss behavior
       -> source update -> new version -> old citation still resolvable
       -> restart -> query recovery
       -> backup -> isolated restore -> lexical query -> generation rebuild
```

## 9. Rollout Flags

Suggested defaults remain off or shadow until evidence exists:

```text
PP_KNOWLEDGE_SYSTEM=off|shadow|on
PP_KNOWLEDGE_INGEST=off|shadow|on
PP_KNOWLEDGE_SEMANTIC=off|shadow|on
PP_KNOWLEDGE_WIKI=off|shadow|on
PP_KNOWLEDGE_RETRIEVAL=off|shadow|on
PP_KNOWLEDGE_LANCE_SHADOW=off|shadow|on
PP_KNOWLEDGE_HOOK=off|shadow|on
PP_KNOWLEDGE_AUTO_DOMAINS=off|shadow|on
PP_KNOWLEDGE_AUTO_PROMOTION=off|shadow|on
PP_KNOWLEDGE_PURGE=off|shadow|on
PP_DEEPSEC_REVIEW=off|shadow|on
PP_EVOLUTION_EVIDENCE=off|shadow|on
PP_EVOLUTION_RULES=off|shadow|on
```

Rollback disables new reads and writes without deleting canonical sources, versions, evidence, artifacts, lineage, jobs, or audit records. Retiring a knowledge generation switches the active manifest; it does not delete the previous generation until retention permits.

### 9.1 DeepSec and self-evolution rollout

Pin DeepSec to audited commit `3742ec0702f6b72956365bee3d23319522db5c40` in the server build manifest, record the wheel/container and rule-bundle SHA-256, and invoke `deepsec shield scan <subject> --layer l1,l2 --format json --output -`. Exit code `2` means active high/critical findings and must be parsed as a valid scan result; other non-zero exits, malformed JSON, missing binaries, and timeouts are degradations. Run with a fresh `DEEPSEC_CONFIG_DIR`, no credentials, a read-only checkout, and denied outbound network. Do not invoke Spear, remote L3, watch mode, auto-fix, suppression mutation, findings upload, remote rules, or DeepSec configuration mutation from review automation.

Roll out in this order:

1. `PP_DEEPSEC_REVIEW=shadow`: run on every review, expose health and findings, but retain the existing verdict while measuring false positives and latency.
2. `PP_DEEPSEC_REVIEW=on`: deterministically block critical/high findings and fail closed for security-sensitive changes.
3. `PP_EVOLUTION_EVIDENCE=shadow`: persist immutable evidence and independence groups, publish knowledge projections, and reconcile without creating candidates.
4. `PP_EVOLUTION_EVIDENCE=on`: allow the reconciler to create candidate rules only with independent reproduction evidence.
5. `PP_EVOLUTION_RULES=shadow`: execute candidate rules against fixtures and live shadow traffic without affecting verdicts, Hook output, or maintenance actions.
6. `PP_EVOLUTION_RULES=on`: allow bounded canary and CAS promotion after rollback evidence exists. Keep the previous active manifest until the retention window closes.

The production evidence ledger belongs to `plastic_memory.db`; cited finding projections belong to `plastic_knowledge.db`. Cross-store publication uses an idempotent outbox. Neither database treats the other store's projection as a new independent source.

## 10. Verification Commands for the Future Implementation

The eventual implementation should provide repository-owned commands equivalent to:

```text
knowledge schema --check
knowledge migrate --dry-run
knowledge ingest-smoke --fixture bilingual-small
knowledge generation build --shadow
knowledge generation reconcile
knowledge evaluate --dataset knowledge-bilingual-v1
knowledge generation canary
knowledge generation promote --evidence <verified-evidence-id>
knowledge backup
knowledge restore-smoke --isolated
knowledge purge --dry-run <source-id>
```

These are required contracts for the implementation plan, not commands that exist today.
