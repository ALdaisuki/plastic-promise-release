# Plastic Promise Knowledge System Architecture

Status: accepted design; knowledge ingestion and async semantic compilation implemented behind rollout gates; Wiki generation remains pending
Date: 2026-08-03

## 1. System Overview

The knowledge system adds a source-grounded, LLM-maintained knowledge layer to Plastic Promise without turning documents into ordinary memories. It follows the LLM Wiki pattern of immutable sources, persistent synthesis, and continuous linting while preserving Plastic Promise's project isolation, evidence lineage, risk gates, audit, and rebuildable indexes.

Goals:

- preserve uploaded and synchronized source material as immutable versions;
- build a persistent, interlinked Wiki and claim graph instead of re-synthesizing every query;
- keep memory and knowledge as distinct truth domains while fusing them in `context_supply`;
- allow models to create and maintain knowledge domains automatically;
- make upload, parsing, semantic analysis, indexing, and promotion asynchronous and recoverable;
- extend the existing Dashboard V2 instead of creating another frontend;
- keep Hook injection under a hard latency budget and off the cloud critical path;
- provide exact source citations, contradiction tracking, controlled physical deletion, and measurable quality gates.
- turn review, code-smell, production, and user-feedback observations into governed self-evolution evidence without permitting self-confirming rule promotion.

Non-goals for the first release:

- no local model runtime on the server;
- no OCR, image understanding, audio transcription, or video understanding;
- no unrestricted web crawler or automatic admission of discovered network content;
- no replacement of `plastic_memory.db`, `memory_recall`, or the existing memory proposal pipeline;
- no public listener for MCP, inference, control, or knowledge ingestion.

The ubiquitous language is recorded in [../../../CONTEXT.md](../../../CONTEXT.md). Key decisions are recorded in [ADR-0001](../../adr/0001-separate-memory-and-knowledge-truth-stores.md), [ADR-0002](../../adr/0002-raw-sources-authoritative-wiki-derived.md), [ADR-0003](../../adr/0003-separate-recall-tools-fuse-context.md), [ADR-0004](../../adr/0004-isolate-knowledge-ingestion-from-mcp.md), and [ADR-0005](../../adr/0005-quarantine-external-findings-before-evolution.md).

## 2. Architecture Diagram

The end-to-end workflow is in [workflow.mermaid](workflow.mermaid).

```text
Mac Dashboard / Codex
  | 19020 -> 9020 MCP + Dashboard
  | 19050 -> 9050 Knowledge Ingestion API
  v
Server loopback runtime
  +-- MCP Server 9020
  |    +-- memory_recall
  |    +-- knowledge_search
  |    +-- context_supply fusion
  |    +-- non-blocking Hook routing
  |
  +-- Knowledge Ingestion API 9050
  |    +-- quarantine upload
  |    +-- source registration and sync
  |    +-- job/status/event stream
  |
  +-- Knowledge Worker
       +-- parse and normalize
       +-- structure-v1 evidence chunks
       +-- async semantic batches
       +-- domain/claim/Wiki maintenance
       +-- validation and risk promotion
       +-- index outbox and generation control
  |
  +-- Evolution Worker
       +-- DeepSec Shield and quality sensors
       +-- immutable evidence ledger
       +-- candidate-rule compiler
       +-- shadow and canary evaluator
       +-- CAS rule promotion and rollback

Canonical state
  +-- plastic_memory.db
  +-- plastic_knowledge.db
  +-- knowledge/blobs/<sha256>

Derived state
  +-- memory LanceDB generations
  +-- knowledge LanceDB generations
```

## 3. Actor and Worker-Role Inventory

The design uses one deterministic coordinator with bounded model roles rather than a free-running multi-agent society.

| Role | Type | Responsibility | Input | Output |
|---|---|---|---|---|
| Human Curator | Human | Uploads sources, reviews high-risk promotion, manages ACL and purge | Source material, review decisions | Authorized source and governance changes |
| Dashboard V2 | Frontend | Presents sources, jobs, domains, Wiki artifacts, claims, conflicts, and review actions | Project-scoped API projections | User commands and read models |
| Ingestion Coordinator | Deterministic coordinator | Owns job state, leases, ordering, retries, idempotency, and promotion orchestration | Source or source-change event | Durable stage transitions |
| Source Adapter | Adapter | Reads upload, folder, Git, URL, or server-path sources without changing domain rules | Source registration | Raw Artifact and source metadata |
| Structure Parser | Deterministic worker | Produces Normalized Documents and stable Evidence Chunks | Raw Artifact | Parser record, normalized text, structure-v1 manifest |
| Semantic Analyzer | Model role | Groups nearby Evidence Chunks into Semantic Units and extracts domains, entities, claims, and contradictions | Bounded JSON batch | Schema-valid analysis JSON with chunk references |
| Wiki Maintainer | Model role | Creates or revises source summaries, entity/concept/topic pages, overviews, and links | Validated semantic analysis and current artifacts | Draft Knowledge Artifact revisions |
| Knowledge Linter | Deterministic plus model role | Finds unsupported claims, broken citations, duplicates, orphans, contradictions, gaps, and stale pages | Candidate artifact graph | Validation findings and repair jobs |
| Promotion Evaluator | Deterministic policy | Applies source, evidence, risk, ACL, quality, and generation gates | Validated candidates | Active, `pending_review`, rejected, stale, or contested state |
| Knowledge Query Engine | Deterministic coordinator | Runs lexical/vector/graph retrieval, evidence hydration, rerank, and citation projection | Typed query and scope | `KnowledgePack` |
| Context Fusion | Existing Plastic Promise module | Allocates memory/knowledge budgets and preserves item type during fusion | `MemoryPack`, `KnowledgePack`, principles, code evidence | Three-layer context package |
| Security Scanner | Deterministic adapter | Runs DeepSec Shield L1/L2 for each review and scheduled project scans | Exact project revision and changed-file manifest | Bounded `SecurityScanResult` with immutable findings or explicit degradation |
| Evolution Coordinator | Deterministic coordinator | Admits evidence, detects independent corroboration, compiles candidates, runs experiments, and controls promotion | Security, quality, operational, knowledge, and human-feedback events | Auditable candidate/rule lifecycle transitions |

## 4. Deep Modules and Interfaces

### 4.1 Knowledge Ingestion

External seam:

```text
submit_source(command) -> Submission
get_job(job_id, project_scope) -> JobView
cancel_job(job_id, project_scope) -> CancellationResult
```

This is a deep module. Callers do not orchestrate parsing, chunking, model batching, domain maintenance, Wiki generation, promotion, or indexing. Those ordering rules and failure modes remain inside the module.

Internal adapters:

- `BlobStore`: filesystem content-addressed adapter and in-memory test adapter;
- `SourceReader`: upload, folder, Git, URL, and in-memory adapters;
- `DocumentParser`: Markdown/text/HTML/PDF/Office/tabular adapters;
- `InferencePort`: existing 9030 cloud gateway adapter and deterministic test adapter;
- `KnowledgeRepository`: SQLite adapter and temporary-SQLite test adapter.

### 4.2 Knowledge Query

External seam:

```text
search(query, scope, budget, evidence_mode) -> KnowledgePack
explain(call_id, scope) -> KnowledgeExplain
```

The interface hides BM25/FTS, vector generation selection, graph expansion, claim conflict policy, rerank, source hydration, and citation formatting. `KnowledgePack` items always identify their kind (`evidence`, `semantic_unit`, `artifact`, or `claim`) and active lifecycle state.

### 4.3 Knowledge Governance

External seam:

```text
review_promotion(command) -> PromotionResult
manage_domain(command) -> DomainChangeResult
resolve_conflict(command) -> ConflictResult
request_purge(command) -> PurgePlan
```

The module owns project ACL, server-authenticated actor identity, risk policy, CAS checks, impact analysis, retention, backups, lineage, and audit. The browser and model never submit authoritative trust tiers or actor identities.

### 4.4 Context Fusion

`context_supply` remains the only public fusion seam. `memory_recall` stays memory-only and `knowledge_search` stays knowledge-only. Fusion accepts typed packs and applies project policy, task intent, budget, and source priority without flattening them into ordinary memory records.

### 4.5 Hook Router

The Hook interface remains small:

```text
before_prompt(event, deadline_ms=400) -> CompactRoutingInjection
after_turn(event) -> MemoryProposalAcknowledgement
```

`before_prompt` uses only local intent routing and the project-session hot cache. It never synchronously calls a cloud model or waits for a cold vector rebuild. A miss produces tool guidance and schedules prefetch. `after_turn` continues to produce memory proposals; it does not ingest conversation text as a knowledge source.

### 4.6 MCP Tool Projection

- `knowledge_search` queries active source-grounded knowledge.
- `knowledge_explain` returns bounded ranking, lifecycle, and citation evidence for a prior search.
- `knowledge_source_submit` registers URL, Git, approved server-path, or internal-experience candidates; binary uploads remain on the 9050 HTTP interface.
- `knowledge_job_status` reads durable ingestion and maintenance job state.
- `knowledge_domain_list` lists active and candidate Knowledge Domains.

Mutation-heavy review, domain maintenance, tombstone, and purge operations remain behind authenticated Dashboard/admin contracts and defense checks rather than broadly exposed MCP tools.

### 4.7 Security Review

External seam:

```text
SecurityScanner.scan(ScanRequest) -> SecurityScanResult
```

`ScanRequest` contains server-resolved `project_id`, `project_root`, base/head revisions, the changed project-relative file manifest, security-sensitivity classification, timeout, and scan ID. `SecurityScanResult` is one of `clean`, `findings`, `degraded`, or `not_applicable`, and returns tool/rule-bundle identity, exact coverage, bounded findings, duration, and a stable failure reason. An empty supported-file set is `not_applicable`, not a synthetic pass.

The production `DeepSecCliAdapter` invokes only Shield scanning from a commit-pinned, network-denied environment with a read-only reviewed checkout and no provider credentials. Every `code-review` prepare scans the changed supported files with local L1/L2 and preserves the DeepSec version, scan identity, project revision, subject hashes, layers, rule IDs, locations, confidence, and bounded evidence. A deterministic fake adapter exercises the same interface in tests. DeepSec MCP remains a future adapter only after its Shield tool contract and output parity are proven; the audited source currently exposes no usable Shield MCP server. Spear penetration commands are never reachable from review automation.

DeepSec native IDs are retained only for traceability because absolute paths and evidence make them unstable across checkouts. Plastic Promise computes its own evidence identity from project-relative path, normalized rule ID, source revision, source-span hash, and pinned rule-bundle hash. Raw matched source never enters prompts, memory, knowledge, PR comments, or ordinary logs; those surfaces receive bounded redacted projections and an evidence hash.

When the enforcement flag is `on`, critical/high findings become blocker/major review findings and cannot be overridden by an LLM review response. In `shadow`, the same scan runs and is persisted as evidence without changing the existing verdict. Exit codes `0` and `2` are valid scans after strict JSON validation; `2` means active high/critical findings. Scanner absence, any other exit code, invalid JSON, timeout, or unsupported output is an explicit degraded result, never a pass. Security-sensitive changes fail closed on degradation only after enforcement promotion; ordinary changes may continue with a visible warning and cannot contribute positive security evidence.

### 4.8 Evolution Evidence

External seam:

```text
EvolutionEvidence.submit(EvolutionEvidenceEvent) -> SubmissionResult
```

`EvolutionEvidenceEvent` contains project and causal scope, sensor identity/version, source revision/hash, subject and rule identity, bounded redacted payload, raw-evidence hash/reference, parent evidence/rule revision, and an idempotency key. `SubmissionResult` returns the immutable evidence ID, `created` or `deduplicated`, the computed independence group, lifecycle state, and queued projection/reconciliation IDs. Callers cannot supply corroboration counts, candidate state, trust tier, actor authority, or an active-rule decision.

The module hides deduplication, provenance normalization, independence grouping, lifecycle checks, cross-store outbox publication, scoring, experiment scheduling, and promotion policy. Submitting evidence never activates a rule. The interface returns the immutable evidence identity, whether it was new or deduplicated, its independence group, and any scheduled reconciliation work without exposing storage orchestration to callers.

## 5. Communication Patterns

| Flow | Pattern | Contract |
|---|---|---|
| Browser upload | Streaming HTTP to 9050 | Authenticated, project-bound, resumable quarantine upload |
| Source processing | Durable SQLite job queue | Lease, heartbeat, bounded retry, idempotency key, dead-letter state |
| Cloud inference | HTTP through 9030 gateway | Provider credentials server-owned; structured JSON schema; timeout/retry/circuit breaker |
| Index update | Transactional outbox | Canonical commit precedes knowledge LanceDB mutation |
| Dashboard progress | SSE or bounded polling from 9050 | Read-only progress events; reconnect from last event ID |
| MCP query | Streamable HTTP on 9020 | `knowledge_search`, `knowledge_explain`, and `context_supply` |
| Cross-store linkage | Idempotent event/outbox | Stable project, source, claim, memory, call, and request-scope IDs |
| Review security scan | Synchronous bounded CLI adapter | Changed supported files only; local L1/L2; bounded JSON; explicit degradation |
| Scheduled security/smell scan | Durable asynchronous job | Full project scan; lease/retry; evidence submission; never blocks request latency |
| Knowledge-to-evolution projection | Transactional outbox and idempotent consumer | Cited claim/finding references; source independence retained; no cross-database transaction |

No cross-SQLite transaction is assumed. A memory derived from knowledge records the knowledge IDs in the memory transaction; the reciprocal knowledge lineage is reconciled idempotently from an outbox event.

## 6. Canonical Data Model

`plastic_knowledge.db` owns these logical records:

| Record | Purpose |
|---|---|
| `knowledge_spaces` | Project-owned access, policy, and active generation identity |
| `knowledge_sources` | Logical upload, folder, Git, URL, or internal-experience origin |
| `knowledge_source_versions` | Immutable content hash, Raw Artifact reference, parser identity, and normalized text |
| `knowledge_chunks` | Stable structure-v1 Evidence Chunks and exact page/section/span anchors |
| `knowledge_semantic_units` | Model-generated groupings bound to Evidence Chunk IDs |
| `knowledge_domains` | Candidate/active/merged/split/retired classification views and aliases |
| `knowledge_domain_bindings` | Primary and secondary domain membership for sources, units, claims, and artifacts |
| `knowledge_claims` | Temporal assertions with confidence, risk, and conflict state |
| `knowledge_claim_evidence` | Supports/refutes/qualifies edges to exact Evidence Chunks |
| `knowledge_artifacts` | Versioned Wiki source/entity/concept/topic/overview/synthesis pages |
| `knowledge_artifact_citations` | Exact citations from artifact revisions to Evidence Chunks |
| `knowledge_ingest_jobs` | Durable stage, lease, retry, failure reason, and idempotency state |
| `knowledge_index_outbox` | Pending derived-index mutations after canonical commits |
| `knowledge_generations` | Embedding/chunking/source-set identity and shadow/canary/production state |
| `knowledge_audit_events` | Server-authenticated operations and lifecycle evidence |
| `knowledge_purge_jobs` | Tombstone, impact plan, backup proof, retention, and purge result |
| `knowledge_security_findings` | Cited projections of admitted Security Findings linked to exact Source Versions and Evidence Chunks |

Raw Artifact bytes live under `/srv/plastic-promise/state/knowledge/blobs/<sha256-prefix>/<sha256>`. The database stores the exact hash and relative reference, never an uncontrolled caller path.

### 6.1 Lifecycle State Machines

```text
Source: candidate -> active -> tombstoned -> purge_eligible -> purged
Artifact: draft -> validated -> active | pending_review
Artifact review: pending_review -> active | rejected
Artifact maintenance: active -> stale | contested
Claim: active -> superseded | contested | unresolved
Domain: candidate -> active -> merged | split | retired
Generation: building -> shadow -> canary -> production -> retired
Job: queued -> leased -> processing -> completed | retry_wait | failed | cancelled
Job retry: retry_wait -> queued
Security finding: observed -> triaged -> confirmed | dismissed | tool_error
Security remediation: confirmed -> fixed -> verified | recurring
Evolution evidence: observed -> corroborated | contested | retired
Evolution rule: candidate_rule -> shadow -> validated -> active -> contested | retired
```

### 6.2 Knowledge and Memory Linkage

- Knowledge used in a decision may produce a memory proposal citing Claim and Evidence Chunk IDs.
- Repeated memories may produce an `internal_experience` Source Candidate after generalization and de-identification.
- Internal experience never counts as an independent external source.
- Provenance traversal rejects self-supporting `knowledge -> memory -> knowledge` cycles and same-session pseudo-consensus.
- Security Findings may become cited knowledge and Evolution Evidence, but neither projection creates a new independent source.
- Rule feedback is linked to the exact rule revision that influenced the decision; its descendants cannot corroborate their ancestor.

## 7. Data Flows

### 7.1 Ingest and Update

1. Register or upload a project-bound Source Candidate.
2. Persist Raw Artifact bytes in quarantine and compute SHA-256.
3. Reuse the existing Source Version for that Source when the content hash is unchanged; distinct Sources may share Raw Artifact bytes but retain distinct Source Versions.
4. Parse the new version into a Normalized Document.
5. Run deterministic `structure-v1` chunking with page, heading, table, code, list, and span anchors.
6. Batch adjacent Evidence Chunks by the same project, source version, and section. Flush at 20 chunks or the configured timeout.
7. Ask the Semantic Analyzer for variable-count JSON units, domains, entities, claims, links, and contradictions. Every result cites input chunk IDs.
8. Create or maintain Knowledge Domains automatically. Merge, split, alias, or retire only through audited lifecycle operations.
9. Create draft Knowledge Artifacts and run structural/source-grounding lint.
10. Promote low-risk validated artifacts automatically. Route high-risk `finance`, `medical`, `legal`, `security`, and `production_operations` artifacts to Curator review.
11. Commit canonical changes and publish index jobs through the outbox.

An updated Source always creates a new Source Version. It never overwrites the active version in place. Only affected chunks, semantic units, claims, artifacts, and index rows are recomputed.

### 7.2 Query and Context Supply

1. Resolve server-owned project scope and ACL.
2. Classify intent as memory, knowledge, mixed, principle, code, or audit.
3. `knowledge_search` runs lexical retrieval, active-generation vector search, graph expansion, risk/stale filters, and optional cloud rerank.
4. Hydrate exact Evidence Chunks for every selected Claim or Knowledge Artifact.
5. Return a typed `KnowledgePack` with citations and conflict state.
6. `context_supply` allocates independent memory and knowledge budgets, fuses ranks, and preserves item type.
7. The answer cites Source Version and Evidence Chunk IDs; generated Wiki artifacts never outrank unsupported source evidence.

### 7.3 Passive Hook

- Hard deadline: 400 ms by default, configurable within the 300-500 ms design range.
- Cache hit: inject bounded knowledge summaries, domains, source IDs, and tool guidance.
- Cache miss: inject only project/domain/tool routing and enqueue an asynchronous prefetch.
- Timeout or backend failure: continue without knowledge content and record a degradation event.
- Stop event: write only governed memory proposals with the knowledge IDs actually used in the turn.

### 7.4 Network Research

Model-discovered URLs enter `source_candidate` quarantine. Fetch metadata, immutable snapshots, source class, publisher identity, and trust evidence before parsing. Unknown web, forum, and social sources start at low trust; high-risk domains require human approval even when analysis passes.

### 7.5 Physical Deletion

1. Tombstone the Source and exclude it from ordinary retrieval.
2. Wait 30 days unless an authorized privacy purge overrides retention.
3. Compute impacted claims, artifacts, memories, and citations.
4. Mark dependencies stale or schedule regeneration.
5. Create a recoverable backup and persist backup evidence.
6. Purge exact IDs and content-addressed blobs; never execute caller-supplied paths.
7. Retain a minimal non-content audit tombstone.

### 7.6 Security Review and Self-Evolution

1. `ReviewEngine.prepare` resolves the exact revision and changed supported files, then invokes DeepSec Shield L1/L2 through `SecurityScanner`.
2. The review prompt receives bounded findings and explicit scanner health; critical/high findings are deterministically merged into the final review gate.
3. The review transaction emits immutable Security Finding events after secret-safe normalization. Scheduled scanners emit architecture, coupling, quality-trend, and full-project security observations through the same evidence seam.
4. `EvolutionEvidence` deduplicates by project, source revision, sensor identity, rule ID, subject hash, and finding fingerprint. It assigns an independence group from causal provenance rather than invocation count.
5. An asynchronous reconciler projects suitable findings into `plastic_knowledge.db` with exact citations and accumulates corroboration. Internal memories, generated Wiki pages, and rule-influenced outcomes preserve ancestry and cannot manufacture independence.
6. A candidate-rule compiler creates an Evolution Candidate only after policy-specific evidence thresholds and an independently reproducible fixture exist.
7. The candidate runs against historical fixtures and live shadow traffic. Shadow cannot mutate review verdicts, scanner settings, memory, knowledge promotion, or production policy.
8. Validated candidates enter a bounded canary. Promotion uses server-owned evidence, scope, risk policy, and CAS activation; rollback restores the prior rule revision without deleting evidence.
9. Active rules influence future review, smell scans, Hook tool guidance, and maintenance scheduling. Every influenced outcome points back to the exact rule revision so effectiveness and regressions can contest or retire it.

## 8. Memory and State Management

| State | Store | Authority |
|---|---|---|
| Memory, proposals, trust, passive capture | `plastic_memory.db` | Existing memory context |
| Evolution evidence, candidates, experiments, active rule manifests | `plastic_memory.db` | Evolution governance context |
| Sources, versions, evidence, domains, claims, Wiki, jobs | `plastic_knowledge.db` | Knowledge context |
| Original bytes | Content-addressed server BlobStore | Knowledge context through SQLite references |
| Memory vectors | Memory LanceDB generation | Derived and rebuildable |
| Knowledge vectors | Knowledge LanceDB generation | Derived and rebuildable |
| Hook cache | Process memory, project-session scoped | Ephemeral and bounded |
| Upload quarantine | Owner-only server directory | Temporary until hash admission or purge |

The Knowledge Generation identity binds embedding provider/model/dimension, chunking identity and budgets, semantic enrichment schema, source revision set, and index policy. A dimension match is insufficient for reuse.

The implemented LanceDB slice is default-off behind `PP_KNOWLEDGE_LANCE_SHADOW`. Both `shadow` and `on` build only a rebuildable, unrouted shadow; they cannot activate, promote, serve queries, or invoke Maintenance, and the Maintenance integration remains unwired. Its current scope is the complete project: `all` is canonical and `knowledge` is an alias. Domain-specific requests fail before provider, filesystem, or generation-table writes until canonical `knowledge_domain_bindings` exist.

Generation identity explicitly binds the complete active project Source Version set (including sources with no emitted vector record), every persisted chunking schema/budget identity, semantic schema and prompt hash, projection version, fusion policy, provider identity/dimension, configuration revision, and projected corpus hash. After vector reconciliation, the builder reloads the canonical SQLite snapshot before recording `shadow`; any source-set, corpus, or chunking change becomes the sanitized `knowledge_lance_canonical_changed` failure. Generation state and its bounded audit event are written atomically in one SQLite transaction.

## 9. Error Handling and Recovery

- Durable jobs use leases and heartbeat renewal; expired leases are recoverable by another worker.
- Parser and model stages record stable failure reason codes without source contents or provider secrets in logs.
- Retry only transient classes with exponential backoff and jitter; validation and policy failures require changed input or review.
- Provider circuit breakers stop repeated cloud failures and preserve lexical/source-only operation.
- Outbox reconciliation compares canonical versions and active generation identity before replay.
- Partial Wiki generation never becomes active. Existing active artifacts remain available until a replacement passes promotion.
- Knowledge search degrades to SQLite lexical retrieval and source hydration when LanceDB or cloud rerank is unavailable.
- Backup and restore tests cover SQLite, BlobStore, generation manifests, and outbox replay. LanceDB is rebuilt rather than treated as backup truth.
- Evolution jobs are idempotent and lease-based. Invalid evidence is quarantined; unavailable sensors create degradations; failed shadow/canary runs keep the current active rule.
- A rule cannot modify its own independence policy, scanner allow/ignore configuration, promotion thresholds, or rollback controls.

## 10. Security Model

### 10.1 Authorization

| Role | Capabilities |
|---|---|
| `viewer` | Search, read source projections, citations, claims, and artifacts |
| `contributor` | Upload and register project-bound Source Candidates |
| `curator` | Review high-risk promotion, conflicts, and domain changes |
| `admin` | Configure connectors, retention, backups, restore, and purge |

Project ACL grants access; roles grant capabilities; Plastic Promise trust only tightens Agent autonomy. Trust cannot grant access to another project.

### 10.2 Runtime Boundary

- MCP/Dashboard: `127.0.0.1:9020`, Mac tunnel `19020`.
- Inference gateway: `127.0.0.1:9030`, separate tunnel when needed.
- Remote configuration: `127.0.0.1:9040`, no knowledge-data writes.
- Knowledge ingestion: proposed `127.0.0.1:9050`, Mac tunnel `19050`.
- No service is exposed through UFW, security groups, or a public reverse proxy.

### 10.3 Input and Secret Safety

- Bearer tokens and provider keys are loaded from mode-0600 EnvironmentFiles and never returned by APIs.
- Dashboard tokens remain in JavaScript memory, not URLs, cookies, or browser storage.
- Uploads enforce size, MIME, extension, archive-depth, decompression-ratio, and path-traversal limits.
- Office macros, embedded executables, and archive contents are never executed.
- URL connectors block loopback, link-local, metadata endpoints, private address ranges, redirects outside policy, and unapproved schemes.
- CORS and Origin checks allow only the configured loopback Dashboard origin.

## 11. Monitoring, Quality, and Scale

### 11.1 Initial Capacity Assumptions

The first production target is the existing 4-vCPU, 16-GiB, 100-GiB server with a small number of human users and multiple Agent/device clients. Defaults:

- 2 concurrent parser jobs;
- 4 concurrent cloud semantic requests;
- semantic batches of 20 Evidence Chunks with a 30-second partial-batch flush;
- 400-ms Hook deadline;
- 15-second internal knowledge-search deadline with lexical degradation;
- 30-day ordinary purge retention.

These are control-plane values, not schema invariants.

### 11.2 Metrics

- ingest queue depth, oldest age, lease recovery, retry and dead-letter counts;
- parser throughput, normalized bytes, chunk count and truncation rate;
- semantic batch utilization, JSON validation failures, provider latency and cost;
- domain creation/merge/split/retire churn and duplicate-domain rate;
- source recall@k, hit@k, MRR, citation precision and citation coverage;
- forbidden cross-project hits and stale/contested leakage, both required to remain zero;
- Hook cache hit, injection rate, timeout rate, bytes and p95 latency;
- generation reconciliation lag and outbox age;
- purge impact, backup evidence, restore duration and rebuild duration.
- DeepSec coverage, scan latency, scanner degradation, finding recurrence, false-positive dismissal, and changed-file coverage;
- evidence independence count, candidate age, fixture reproduction, shadow precision/recall delta, canary regressions, active-rule rollback, and self-cycle rejection count.

### 11.3 Promotion Gates

Every embedding, chunking, enrichment, or fusion identity change follows:

```text
building -> shadow rebuild -> reconciliation -> fixed bilingual evaluation
         -> canary queries -> production CAS promotion
```

The gate requires source recall, citation correctness, project isolation, stale-content exclusion, latency, provider cost metadata, restart recovery, and one restore/rebuild rehearsal.

## 12. Technology and Cost Model

| Layer | Choice |
|---|---|
| Runtime | Existing Python 3.12 environment; optional Rust acceleration after parity evidence |
| Web | Existing Starlette/uvicorn conventions with a separate 9050 process |
| Canonical knowledge | SQLite WAL plus content-addressed filesystem BlobStore |
| Derived retrieval | Dedicated LanceDB knowledge generations plus SQLite lexical fallback |
| Parsing | Structured adapters selected per media type; deterministic text/structure output |
| Models | Existing 9030 OpenAI-compatible cloud gateway; one embedding identity per generation; task/risk-based analysis models |
| Frontend | Existing Dashboard V2 modules and routing |

Cost is computed from server-owned price metadata rather than hard-coded vendor prices:

```text
embedding_cost = embedded_tokens / 1_000_000 * embedding_price
analysis_cost = input_tokens / 1_000_000 * input_price
              + output_tokens / 1_000_000 * output_price
monthly_cost = sum(ingest + maintenance + query_rerank + rebuild)
```

SHA reuse, incremental versions, 20-chunk batching, content-hash caches, lexical fallback, and model routing are mandatory cost controls. The Dashboard reports actual cost by project, domain, source version, model identity, and job stage.

## 13. Implementation Phases

1. **Canonical foundation**: schema, BlobStore, source/version lifecycle, quarantine, audit, backup, and REST read models.
2. **Deterministic ingestion**: parsers, normalized documents, structure-v1 Evidence Chunks, durable jobs, leases, retries, and incremental versioning.
3. **Semantic compilation**: batched JSON analysis, automatic Knowledge Domains, claims, contradictions, Wiki artifacts, lint, and risk promotion.
4. **Retrieval integration**: knowledge BM25/FTS, cloud embedding shadow generation, `knowledge_search`, explain, and `context_supply` fusion.
5. **Hook and Dashboard**: non-blocking routing/prefetch, upload/source/job/domain/artifact/conflict modules, and project ACL.
6. **Production gates**: fixed bilingual evaluation, canary, generation promotion, backup/restore rehearsal, purge workflow, observability, and runbooks.
7. **Multimodal extension**: OCR, image/chart understanding, audio/video transcription, and time-based citations after the text system is stable.
8. **Governed self-evolution**: DeepSec review adapter, evidence ledger, knowledge projection outbox, candidate compiler, fixture/shadow/canary evaluation, active-rule manifest, rollback, and Dashboard evidence lineage.
