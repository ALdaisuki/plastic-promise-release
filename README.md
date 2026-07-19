<!-- SEO Meta Tags
Description: Plastic Promise — local-first MCP runtime for AI agent memory, context supply, audit, trust, skills, and governed task dispatch. Commitment Engineering turns operating agreements into retrievable, traceable agent behavior.
Keywords: ai-governance, mcp-server, agent-memory, commitment-engineering, context-engine, llm-agent, multi-agent, trust-score, memory-decay, lancedb
Author: ALdaisuki
Canonical: https://github.com/ALdaisuki/plastic-promise-release
-->

<!-- Open Graph / Twitter
og:type: website
og:url: https://github.com/ALdaisuki/plastic-promise-release
og:title: Plastic Promise - Local-first MCP governance runtime
og:description: Local-first memory, context supply, audit, trust, skills, and governed task dispatch for MCP agents.
twitter:card: summary
twitter:title: Plastic Promise - Local-first MCP governance runtime
twitter:description: Local-first memory, context supply, audit, trust, skills, and governed task dispatch for MCP agents.
-->

<!-- GitHub Metadata
topics: ai-governance, mcp-server, agent-memory, multi-agent, local-first, lancedb, sqlite, rust-python
languages: Python, Rust
-->

<div align="center">

# Plastic Promise

### Local-first memory, context, audit, and task governance for MCP agents

中文版本: [docs/README.zh-CN.md](docs/README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/plastic-promise?style=flat-square&label=PyPI)](https://pypi.org/project/plastic-promise/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white&style=flat-square)](https://www.python.org/)
[![Rust](https://img.shields.io/badge/rust-optional_core-000000?logo=rust&logoColor=white&style=flat-square)](https://www.rust-lang.org/)
[![MCP](https://img.shields.io/badge/protocol-MCP_1.0-FF6B35?style=flat-square)](https://modelcontextprotocol.io/)
[![License](https://img.shields.io/badge/license-MIT-yellow?style=flat-square)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha-red?style=flat-square)](#status)

![SQLite](https://img.shields.io/badge/storage-SQLite_WAL-003B57?logo=sqlite&logoColor=white&style=flat-square)
![LanceDB](https://img.shields.io/badge/vector_store-LanceDB-3B82F6?style=flat-square)
![Ollama](https://img.shields.io/badge/default_embedding-Ollama_mxbai--embed--large-111827?style=flat-square)
![Local First](https://img.shields.io/badge/data-local_first_by_default-16A34A?style=flat-square)

[Quick Start](#quick-start) · [Architecture](#architecture) · [Core Modules](#core-modules) · [Documentation](#documentation) · [Roadmap](docs/TODO%20List/README.md)

</div>

---

**Plastic Promise** is a local-first governance runtime for AI agents. It exposes memory, context supply, audit, trust, skill tracking, and task-dispatch capabilities through an MCP server, backed by SQLite and LanceDB.

The project is built around **Commitment Engineering**: instead of relying only on hard gates, an agent retrieves the relevant agreements, prior decisions, trust state, and verification rituals before it acts. The goal is not to block every mistake at the edge; the goal is to make useful behavior repeatable, traceable, reviewable, and self-improving.

---

## Who it is for

Plastic Promise is for developers and agent teams that need more than a one-off memory store. It is useful when an MCP client, coding agent, or multi-agent workflow needs shared memory, explicit governance rules, auditable task handoff, and a local-first runtime that can explain what context was used before an action.

It is intentionally biased toward operational traceability:

| Need | Plastic Promise answer |
|---|---|
| Agents forget decisions between sessions | Store and retrieve memories with worth, decay, deduplication, and graph links. |
| Context retrieval is inconsistent | Use `context_supply` to produce a structured core/related/divergent context package. |
| Automation needs guardrails | Run defense, audit, trust, and principle checks before shared-state changes. |
| Multi-agent work is hard to verify | Route work through Hunter Guild claim, heartbeat, completion, and verification states. |
| Workflows become prompt folklore | Turn startup, remembering, closure, review, and SuperPowers stages into MCP tools. |

---

## What it does

| Capability | What it provides |
|---|---|
| Agent memory | Stores experience, facts, decisions, entities, events, and patterns with quality gates and decay. |
| Context supply | Builds task-specific context packages from vector, text, symbolic, graph, worth, recency, project policy, and recommendation signals. |
| Audit and defense | Checks actions against hard boundaries, trust tiers, tool manifests, and audit dimensions before shared-state changes. |
| Trust-driven autonomy | Maps observed reliability to autonomy, review requirements, and task-claim permissions. |
| Skills and closure | Tracks reusable workflows and step-closure reflections so lessons feed future work. |
| Hunter Guild dispatch | Routes work through a claim, heartbeat, completion, and verification lifecycle. |
| Extensions and market | Loads optional knowledge, workflow, capability, and adapter packs through validated metadata. |

---

## Architecture

<p align="center">
  <img src="docs/architecture/plastic-promise-flow.svg" alt="Plastic Promise local governance runtime architecture" width="960">
</p>

The README-level vector diagram shows the runtime in five layers: actors, MCP entrypoints, governance core, automation loop, and local persistence/acceleration. It is intentionally higher level than the C4 files so the first architecture view stays readable on GitHub.

Full architecture diagrams:

- [Vector overview - English](docs/architecture/plastic-promise-flow.svg)
- [Vector overview - Chinese](docs/architecture/plastic-promise-flow.zh-CN.svg)
- [C4 Level 1 — Context](docs/architecture/diagrams/c4-level1-context.txt)
- [C4 Level 2 — Container](docs/architecture/diagrams/c4-level2-container.txt)
- [C4 Level 3 — Component](docs/architecture/diagrams/c4-level3-component.txt)
- [Sequence diagram](docs/architecture/diagrams/sequence.mermaid)
- [Component diagram](docs/architecture/diagrams/components.mermaid)

---

## Quick Start

### Install

```bash
# From PyPI
pip install plastic-promise

# From source
git clone https://github.com/ALdaisuki/plastic-promise-release.git
cd plastic-promise-release
pip install -e ".[dev]"
```

Optional Rust accelerator:

```bash
cd rust/context-engine-core
pip install maturin
maturin develop --release
```

### Start the runtime

```bash
# One-click launcher: MCP server (:9020) + maintenance daemon + watchdog
python scripts/init_and_start.py

# Non-interactive startup can pin a runtime mode
python scripts/init_and_start.py --mode rust-full

# If Ollama is unavailable, use fallback embedding mode
python scripts/init_and_start.py --skip-ollama-check
```

If no mode is provided in an interactive terminal, the launcher asks which runtime mode to use before it starts services. Non-interactive startup defaults to `rust-full` to preserve the Rust-first full warmup path.

The one-click launcher and direct MCP entrypoint set `PLASTIC_PROJECT_ID=project:plastic-promise` unless `PLASTIC_PROJECT_ID` or `PP_PROJECT_ID` is already set. Direct MCP starts can still override either key explicitly so `memory_recall` and `context_supply` keep core and related context in the intended project boundary instead of degrading to `project:unknown`.

| Mode | Rust supply | LanceDB startup warmup | Typical use |
|---|---:|---:|---|
| `light` | no | no | Fastest startup; defer LanceDB and use the Python path. |
| `normal` | no | no | Python path with lazy LanceDB init available later. |
| `rust-normal` | yes | no | Rust-first context supply without startup rebuild. |
| `full` | no | yes | Python path plus LanceDB init/backfill/rebuild on startup. |
| `rust-full` | yes | yes | Rust-first context supply plus full startup LanceDB maintenance. |

For `full` and `rust-full`, the backfill/rebuild work belongs to launcher startup warmup. Once the MCP process is running, request-time heavy initialization opens the LanceDB/domain backends but should keep `LDB_BACKFILL_ON_INIT=0` and `LDB_REBUILD_ON_INIT=0` so a normal `context_supply` or debug recall cannot rerun maintenance inside the hot request path.

After startup, MCP clients can inspect or hot-switch the process mode with `runtime_mode(action="get")` and `runtime_mode(action="set", mode="rust-normal")`.

The launcher prepends the project root to child-process `PYTHONPATH`, so script services such as the Maintenance Daemon import the same local package tree as the MCP Server. The daemon also self-bootstraps its project root for direct starts.

Run only the MCP server:

```bash
# stdio mode
python -m plastic_promise

# Streamable HTTP mode on port 9020
python -m plastic_promise --streamable-http 9020

# Legacy alias, still supported for older scripts
python -m plastic_promise --sse 9020
```

Run only the Maintenance Daemon after an MCP Server is already available:

```bash
python daemons/maintenance_daemon.py
```

Health check:

```bash
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9020/health').read())"
```

`/health` is also the deployment identity contract. It returns `pid`,
`source_root`, `source_revision`, `fusion_policy`, and `fusion_attestation`;
the attestation carries `schema=retrieval-fusion-identity/v1`, the requested
policy, candidate ID, and configuration hash. The launcher accepts a newly
started server only when health matches the spawned PID and current source root,
plus the expected Git revision when available. It reuses an existing process on
port 9020 only after the same source-root/revision checks pass; HTTP 200 alone is
not ownership evidence.

On Windows, `python scripts/init_and_start.py --stop` reads only
`var/run/mcp_server.pid` and `var/run/maintenance_daemon.pid` from the current
checkout, then verifies the command line contains that checkout's source root.
It does not scan for or terminate other Python processes or other worktrees.

### Connect an MCP client

Stdio example:

```json
{
  "mcpServers": {
    "plastic-promise": {
      "command": "python",
      "args": ["-m", "plastic_promise"]
    }
  }
}
```

Claude Code project config example (`.mcp.json` in a trusted checkout):

```json
{
  "mcpServers": {
    "plastic-promise": {
      "type": "http",
      "url": "http://127.0.0.1:9020/mcp"
    }
  }
}
```

Codex project config example (`.codex/config.toml` in a trusted checkout):

```toml
[mcp_servers.plastic_promise]
url = "http://127.0.0.1:9020/mcp"
startup_timeout_sec = 120
tool_timeout_sec = 120

[profiles.stdio-fallback.mcp_servers.plastic_promise]
command = "python"
args = ["-m", "plastic_promise"]
startup_timeout_sec = 120
tool_timeout_sec = 120

[profiles.stdio-fallback.mcp_servers.plastic_promise.env]
PYTHONIOENCODING = "utf-8"
PLASTIC_DB_PATH = "data\\db\\plastic_memory.db"
PLASTIC_LANCEDB_PATH = "data\\lancedb"
```

Modern shared MCP clients should connect to:

```text
http://127.0.0.1:9020/mcp
```

Legacy SSE clients can still connect to:

```text
http://127.0.0.1:9020/sse
```

---

## First useful calls

```text
session-init(task_description="start a governed coding session", context_mode="light")
memory_recall(query="release documentation", task_type="architecture")
context_supply(task_description="update README", task_type="architecture")
audit_pre_check(action_description="write docs", action_type="write")
memory_store(content="decision and rationale", memory_type="experience")
step-closure(task_description="completed docs update", mode="full", ...)
```

Hunter Guild lifecycle:

```text
task_enqueue -> task_claim -> task_heartbeat -> task_complete -> task_verify
```

---

## Core Modules

This module map follows a capability-first layout so readers can understand the system before reading source folders.

| Module group | Source area | Responsibility |
|---|---|---|
| MCP server | `plastic_promise/mcp/` | Declares tool schemas, stdio/Streamable HTTP entrypoints, legacy SSE compatibility, health endpoints, dashboard, prompts, and resources. |
| Context engine | `plastic_promise/core/context_engine.py` | Supplies layered context by combining retrieval, graph, principle, ranking, and degraded-mode signals. |
| Memory pipeline | `plastic_promise/memory/`, `plastic_promise/memory/pipeline.py` | Extracts, classifies, deduplicates, quality-scores, embeds, stores, reinforces, merges, and decays memories. |
| Storage layer | `plastic_promise/core/lancedb_store.py`, SQLite paths | Stores structured state in SQLite and vector/search state in LanceDB. |
| Principles and graph | `plastic_promise/core/principles.py`, `plastic_promise/principles/` | Activates, evaluates, and links operating principles to memory and context. |
| Audit, defense, trust | `plastic_promise/defense/`, `plastic_promise/core/step_auditor.py`, `plastic_promise/core/tool_manifest.py` | Enforces hard boundaries, trust tiers, tool semantic decisions, audit reports, and pre-action checks. |
| Skills and workflow | `plastic_promise/skills/`, `plastic_promise/loop/` | Implements session lifecycle, smart remembering, step closure, and SuperPowers stage integration. |
| Hunter Guild dispatch | `plastic_promise/mcp/tools/task_queue.py`, `plastic_promise/core/task_*` | Manages task posting, claiming, heartbeat, completion, verification, and failure penalties. |
| Daemons and launcher | `scripts/init_and_start.py`, `daemons/maintenance_daemon.py`, `plastic_promise/launcher/` | Starts services, watches health, runs scans, and recovers routine lifecycle issues. |
| Extensions and market | `plastic_promise/extensions/`, `plugins/` | Loads optional packs through validated metadata without importing untrusted code during validation. |
| Rust context core | `rust/context-engine-core/` | Optional PyO3 acceleration path. Rust snapshot ingestion filters audit telemetry before indexing, and Python keeps a final native-result guard while parity evolves. |

---

## MCP Tool Surface

The current source exposes **58 MCP tools** in `plastic_promise/mcp/server.py`, including compatibility aliases such as `session_init` for `session-init`. Older documents may mention 48, 51, 56, or 57; those counts predate the runtime mode tool, market tools, review tools, commercial audit export, MGP shadow bridge, and alias surface.

| Group | Tools |
|---|---|
| Memory | `memory_recall`, `memory_store`, `memory_update`, `memory_forget`, `memory_list`, `memory_gc`, `memory_correct`, `memory_reclassify`, `memory_sync_files` |
| Principles | `principle_activate`, `principle_evaluate` |
| Context | `context_supply`, `context_inject`, `context_graph`, `auto_context_inject` |
| Audit and defense | `audit_run`, `audit_pre_check`, `defense` (`evaluate_tool` explains `allow`, `ask`, or `deny` from tool manifest metadata) |
| Reflection | `scarf_reflect`, `feedback_apply` |
| System and runtime | `system`, `runtime_mode`, `issue_create`, `issue_transition`, `issue_list` |
| Experience packs | `pack_export`, `pack_import` |
| Domain federation | `domain` |
| Dispatch | `task_enqueue`, `task_claim`, `task_complete`, `task_verify`, `task_inbox`, `task_heartbeat`, `task_abandon` |
| Skill tracking | `skill_session_start`, `skill_session_complete`, `skill_session_trace`, `skill_session_audit`, `skill_auto_track` |
| Programmatic skills | `session-init`, `smart-remember`, `step-closure` |
| Review | `review_run` |
| Commercial audit | `commercial_audit_export` |
| MGP shadow | `mgp_shadow_bridge` |
| Market | `market_list`, `market_install`, `market_upgrade`, `market_remove`, `market_enable`, `market_disable`, `market_status` |
| SuperPowers | `sp-stage` |

`sp-stage` keeps the 16-stage workflow as a compact programmatic governance contract. Clients receive only the current stage and route, required artifacts, and a closure reminder; the server validates every requested transition and returns valid successors when it rejects a chain violation. Clients do not need to load or reproduce detailed SuperPowers skill instructions. Session/flow isolation, review, audit, trust checks, and traceability remain enforced by the runtime.

---

## Core Concepts

### Commitment Engineering

Plastic Promise treats agreements as living context. Agents are expected to retrieve relevant commitments before they act, explain degradation when context is missing, and close the loop after substantive output.

### Memory quality pipeline

```text
capture -> extract -> classify -> embed -> deduplicate -> quality gate -> decay init -> retrieve
```

Memory is admitted only when it passes quality checks. Reuse increases worth; stale or duplicated memories can decay, merge, or be forgotten.

Long memories are embedded through bounded chunks before they are written to
LanceDB. The default local embedding model remains Ollama `mxbai-embed-large`,
but oversized review/audit text is split by `EMBEDDER_CHUNK_CHARS`, capped by
`EMBEDDER_MAX_CHUNKS`, mean-pooled, and normalized so a single large record does
not turn into an Ollama 500 during launcher warmup or backfill.

`PP_MEMORY_CHUNKING=shadow` keeps the legacy embedding requests and index identity,
while recording a deterministic structure-aware candidate manifest for comparison.
`PP_MEMORY_CHUNKING=structure-v1` enables that structural baseline for Ollama
embedding input. It recognizes Markdown heading paths,
paragraphs, fenced code, lists, and tables; isolates atomic blocks; preserves
verbatim source spans; and processes the complete tail within the bounded
`EMBEDDER_STRUCTURE_MAX_CHUNKS` request budget. When the budget is exceeded, it
keeps the beginning and tail and marks the middle coverage as resource-limited.
`EMBEDDER_CHUNK_CHARS` becomes the soft packing target and
`EMBEDDER_STRUCTURE_HARD_CHARS` is the oversized-block limit. The current budget
unit is explicitly `characters-fallback` because the Ollama embeddings endpoint
does not expose model tokenizer counts. `EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS`
is a hard input guard. The mode remains off by default; shadow does not create
child rows or change retrieval identity, while structure-v1 binds all chunking
configuration into the persisted embedding model identity so enabling or
rolling back the active baseline triggers derived-index migration.

After `structure-v1` has produced canonical chunks, an optional local semantic
enrichment layer can add retrieval-only metadata without changing chunk text,
order, heading paths, or source spans. Set `PP_MEMORY_CHUNK_ENRICHMENT=shadow`
to enqueue bounded daemon analysis with Ollama `qwen3:8b`; vectors and index
identity remain unchanged. Valid results are stored in a content-addressed
SQLite cache adjacent to the canonical database by default. Set the mode to
`on` for the initial offline rebuild or migration. Once that derived index
identity is serving, keep `on` enabled so new document writes and index repairs
synchronously prepare the same exact plan; query embeddings never invoke the
enrichment model. Validated summaries, keywords, entities, and identifiers are
prepended to derived embedding input, and the model, prompt, and schema versions
are bound into index identity. Pin `PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST`
when reproducible deployment identity is required; otherwise the digest is
resolved from Ollama `/api/tags`.

The Ollama `/api/chat` request disables thinking, uses temperature zero, and
requests a strict JSON Schema. The response is still independently validated:
unknown or missing fields, a non-verbatim summary/evidence/keyword/entity,
identifier mismatches, invalid JSON, timeouts, and unavailable models all fail
closed to the original chunk. The
default remains `off`; enrichment is inactive unless
`PP_MEMORY_CHUNKING=structure-v1` is also enabled.

The read-only shadow report can be run against the canonical SQLite memories
or an explicit JSON/JSONL corpus. It reports truncation, candidate coverage,
block kinds, chunk-count ratio, and local planning latency without calling an
embedding model or writing any index:

```powershell
python scripts/benchmark_chunking_shadow.py --source data/db/plastic_memory.db
python scripts/benchmark_chunking_shadow.py --source tests/fixtures/recall_quality/v1.json
```

The report keeps record ids and diagnostics only; source text is not emitted.
Use the report to choose the next real-model recall benchmark, not as a release
quality conclusion by itself.

`PP_MEMORY_SUMMARY_INDEX=1` enables the feature-gated summary index write path.
SQLite remains the truth source for `raw_content`, L0/L1/L2 summary layers, and
the exact summary-only `embedding_text` / `embedding_hash` used for indexing.
LanceDB remains a derived index: it receives the vector plus compact
`search_text`, not the raw turn or full L2 narrative. With the flag unset, the
legacy LanceDB `text=content` behavior is preserved.

### Context supply

`context_supply` produces a layered context package for a task. It combines semantic search, text search, graph links, principles, and ranking signals into core, related, and divergent context.

`session-init` stays lightweight and does not run full `context_supply` automatically. Its `context_mode` is `light` by default: it may return a bounded 1-2 item lexical memory preview, but material planning, code edits, reviews, and subagent dispatch still require an explicit `memory_recall` / `context_supply` call. Use `context_mode="none"` for pure bootstrap and `context_mode="full"` only when startup-time full retrieval is intentional.

Concurrent heavy context calls can carry `stage_session_id`, `flow_line_id`, and `request_id`. Plastic Promise derives a `request_scope_id` from those fields, includes it in audit metadata and `context_supply` output, and uses it to isolate `memory_recall` cache entries across overlapping SuperPowers stages or agent flows.

`memory_recall` and `context_supply` also return context recommender metadata. Recommendations include stable reasons and ranking inputs, but they annotate the selected context instead of bypassing project policy, exclusions, trust boundaries, or explicit retrieval budgets.

In `rust-full`, `memory_recall(debug=true)` stays on the Rust snapshot hot path when Rust is healthy and preferred. Debug recall still returns Rust `pipeline_stats` and `per_item_stats`, and only falls back to Python if the Rust path is unavailable or throws. When LanceDB rows exist, debug `pipeline_stats` should report a nonzero `vector_count`; `vector_hits` may be zero only when the query has no vector match.

`context_supply(debug=true)` is the MCP-facing diagnostic path for context
pack construction. Normal `context_supply` calls still return prompt text, while
debug calls return structured JSON containing the prompt, selected layers,
`audit_metadata`, `pipeline_stats`, and `per_item_stats`. The Engram-inspired
canonical hot lookup and ContextGate instrumentation are off by default and can
be observed with `PP_CANONICAL_HOT_LOOKUP=1` and `PP_CONTEXT_GATE=1`; prompt
layers only change when the separate enforcement flags are explicitly enabled.

### Step closure

`step-closure` records what changed, what was learned, why it happened, and what should improve next. That reflection updates memory and trust signals.

### Trust-score-driven autonomy

Trust is persisted and changes over time. Higher trust allows more autonomy; lower trust requires more explicit approval or read-only behavior.

### Governance runtime events

Tool calls and Hunter Guild task transitions are recorded as `runtime_events` with `pending`, `running`, `completed`, or `error` status plus request scope, trust tier, defense decision, and audit trace metadata. These events complement span logs by preserving state transitions that can be replayed or audited.

### MGP shadow bridge

`mgp_shadow_bridge` maps MGP-like memory governance operations to Plastic Promise semantics. P1 mode is audit-first: `shadow` records policy decisions without mutating memory, and `inject` is reserved for a later phase.

### Explicit degraded mode

Local storage is the default. Optional external calls depend on configured agents, embedding providers, rerankers, or LLM integrations. If optional services are unavailable, Plastic Promise uses degraded mode and should label uncertainty instead of silently pretending the full path ran.

Embedding and reranking are separate local model roles. `mxbai-embed-large` is an
embedding-only model used for vectors; the default local Ollama reranker uses a
generation-capable model (`qwen2.5:3b`) before falling back to cosine/original
ordering. Hosted rerankers remain opt-in through `PP_RERANK_PROVIDERS`.

### Governed synthesis and memory proposals

Governed synthesis is opt-in and fail-closed. SQLite owns canonical memory,
lifecycle, provenance, proposal review, and exact index material; LanceDB is a
rebuildable derived index. The default gates preserve legacy behavior:

| Gate | Default | Effect when enabled |
|---|---|---|
| `PP_SYNTHESIS_ARTIFACTS` | `off` | `shadow` evaluates eligibility without creating artifacts; `on` permits governed drafts. |
| `PP_SYNTHESIS_RETRIEVAL` | `0` | `1` admits only current `verified` synthesis with complete verification evidence. |
| `PP_MEMORY_PROPOSALS` | `off` | `shadow` emits hash-only diagnostics; `on` routes public user facts, preferences, and decisions to review. |
| `PP_MEMORY_INDEX_TEXT_POLICY` | `legacy` | `compact-v2` is an experimental bounded L0/L1 index-text candidate. |

The synthesis lifecycle is `draft -> verified -> stale|contested`. Refreshing a
stale or contested artifact creates the next `draft` revision, which must be
verified again. Verification requires non-empty `last_verified_at`,
`verified_by_actor`, and `verified_by_call_id`; retrieval treats missing control
state or evidence as unavailable. High-impact context plans expand sources only
for synthesis selected into the final context layers.

Retrieval-visible ordinary-memory mutations use a canonical field-scoped
transaction. Content replacement or unavailability records source lineage,
marks dependent synthesis stale, increments the canonical memory version, and
persists checked index jobs before commit. GC merge candidates must have the
same non-empty project, and the coordinator rechecks both declared and canonical
project identity inside the transaction. A mismatch fails without partial
memory, lineage, version, outbox, or cache changes.

Public mutation identity and authority come from server-owned runtime context.
Caller-declared actor, call, project, or trust fields are audit input only; both
`smart-remember` aliases require `memory_update` authority before reading or
changing an existing canonical row. Public `memory_forget` remains a critical
operation with a `0.80` trust requirement. The lower `0.60` `audit_rollover`
capability is internal and does not weaken the public delete boundary.

Proposal review records the reviewer actor, call ID, review time, and a stable
reason code. Pending, rejected, and expired proposals never become recall
candidates or LanceDB rows. The maintenance daemon runs canonical memory
lifecycle updates, proposal expiry, synthesis integrity invalidation, synthesis
index replay, then audit, in that order.

Deterministic recall reports validate metric math and policy gates but are never
publishable quality evidence. A publishable comparison requires isolated seeding
of the same versioned bilingual corpus, the same real non-fallback embedding
model and dimension, equal runtime/warmup/repeat metadata, complete equal split
sets, and a successful store-to-recall-to-context smoke check.

Fusion defaults to `legacy-auto`. `max-v1` is the fixed comparison baseline;
an adopted weighted policy is identified as `wrrf-v1:<sha256>` and must match a
frozen candidate manifest. Bare `wrrf-v1` is accepted by the benchmark CLI only
with `--candidate-manifest`; unknown, unhashed, mismatched, or malformed policy
configuration fails closed. Calibration fingerprints held-out bytes before the
manifest is frozen but does not load or query held-out cases.

For `0.1.15`, the one-shot public calibration produced no eligible WRRF
candidate. The held-out cases therefore remained unopened and the released
fusion policy stays `legacy-auto`; no measured fusion-improvement claim is made.

Maintenance supports a production-equivalent one-shot cycle and a real restart
recovery proof:

```bash
python daemons/maintenance_daemon.py --once --json
python scripts/smoke_restart_recovery.py --artifact-dir .artifacts/recovery-smoke --json
```

The launcher health check treats `maintenance-heartbeat/v1` as the daemon
liveness contract and binds it to the daemon PID before falling back to legacy
mtime checks. Checked ordinary index replay reads existing valid
`memory-index/v2` upserts for compatibility; every new upsert or delete is
written as `memory-index/v3` with action, project, memory version, material
revision, and expected embedding hash.

Operational rollback disables all new behavior without deleting canonical
control, provenance, proposal, or audit rows:

```bash
PP_SYNTHESIS_RETRIEVAL=0
PP_SYNTHESIS_ARTIFACTS=off
PP_MEMORY_PROPOSALS=off
PP_MEMORY_INDEX_TEXT_POLICY=legacy
PP_RETRIEVAL_FUSION_POLICY=legacy-auto
```

Also unset `PP_RETRIEVAL_RRF_K`, `PP_RETRIEVAL_RRF_WEIGHTS_JSON`, and
`PP_RETRIEVAL_RRF_WINDOWS_JSON`. Keep SQLite, provenance, and outbox rows;
restart both processes, run one-shot maintenance to replay the default checked
index policy, then run the HTTP and restart-recovery smokes.

For an upgrade to `0.1.18`, leave these gates at their defaults until the live
deployment passes its project-isolated smoke checks. Restart the MCP server and
Maintenance Daemon together so every writer uses the same canonical mutation
contract. No public MCP tool or parameter was removed; existing SQLite memory
remains canonical and LanceDB can be repaired from durable checked jobs. The
minimum LanceDB version is now `0.34.0`; deployments pinned below that version
must upgrade the dependency before restart.

When changing `PP_MEMORY_CHUNKING`, rebuild the derived index before enabling
traffic, and repeat the rebuild after rollback to `off`:

```powershell
$env:PP_MEMORY_CHUNKING = "structure-v1"
python scripts/rebuild_lancedb.py
$env:PP_MEMORY_CHUNKING = "off"
python scripts/rebuild_lancedb.py
```

Roll out semantic enrichment in two phases. Shadow mode can run with normal
traffic because it does not change vectors. Active mode starts with an offline
rebuild, then remains enabled while serving the enriched index:

```powershell
$env:PP_MEMORY_CHUNKING = "structure-v1"
$env:PP_MEMORY_CHUNK_ENRICHMENT = "shadow"
# Run representative writes/backfills, then inspect enrichment diagnostics/cache.

$env:PP_MEMORY_CHUNK_ENRICHMENT = "on"
python scripts/rebuild_lancedb.py

# Rollback preserves canonical SQLite content; disable enrichment and rebuild
# the derived index to return to the legacy index identity.
$env:PP_MEMORY_CHUNK_ENRICHMENT = "off"
python scripts/rebuild_lancedb.py
```

---

## Configuration Notes

| Area | Default |
|---|---|
| MCP server port | `9020` for Streamable HTTP mode (`/mcp`) |
| Server entrypoint | `python -m plastic_promise` |
| One-click launcher | `python scripts/init_and_start.py` |
| Launcher modes | `light`, `normal`, `rust-normal`, `full`, `rust-full`; non-interactive default is `rust-full` |
| Maintenance daemon | `daemons/maintenance_daemon.py` |
| Default local embedding path | Ollama `mxbai-embed-large`, with chunked long-text pooling and fallback embedder when configured |
| Optional chunk enrichment | Off by default; local Ollama `qwen3:8b`, strict grounded schema, SQLite cache; `on` is activated with an offline rebuild and stays enabled for matching writes/repairs |
| Structured database | `data/db/plastic_memory.db` unless `PLASTIC_DB_PATH` overrides it |
| Vector database | `data/lancedb` unless `PLASTIC_LANCEDB_PATH` overrides it |
| Codex repo skills | `.agents/skills/*/SKILL.md` |
| Reranker providers | Local Ollama generation model `qwen2.5:3b` plus cosine fallback by default; hosted providers require `PP_RERANK_PROVIDERS` opt-in |
| Runtime logs and PIDs | `var/log/`, `var/run/` |

Service subprocesses inherit the launcher's runtime-mode environment and receive the project root at the front of `PYTHONPATH`; this keeps direct script entrypoints and hidden Windows subprocesses aligned with source-checkout execution.

Privacy boundary: Plastic Promise is local-first by default. Data can leave the machine only when you configure external agents, hosted embedding providers, hosted rerankers, or other network integrations.

---

## Roadmap Snapshot

The current roadmap lives in [docs/TODO List/README.md](docs/TODO%20List/README.md). At a high level, active work is organized around:

| Track | Direction |
|---|---|
| Runtime reliability | Keep `session-init`, `context_supply`, `runtime_mode`, daemon startup, and degraded-mode behavior predictable under light and full modes. |
| Rust acceleration | Continue converging the optional Rust context-engine path with the canonical Python pipeline; rebuild and import-test the release PyO3 module after Rust changes. |
| Hunter Guild | Harden task queue policy, scanner quality, reassignment, verification, and trust-score effects. |
| Extension market | Stabilize pack validation, install/enable/disable flows, and plugin metadata boundaries. |
| Public documentation | Keep README, architecture docs, quickstarts, and roadmap entries aligned with source truth. Future release docs should maintain English and Chinese coverage together. |

Known status is summarized below; unfinished detail remains in the roadmap document rather than expanding this README into a full project manual.

---

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check plastic_promise/
```

Makefile shortcuts are available for common local workflows:

```bash
make dev-install
make test-fast
make lint
make check
```

Optional service checks:

```bash
python scripts/init_and_start.py --check-only
python scripts/init_and_start.py --skip-ollama-check --check-only

# Verify the live Streamable HTTP MCP process after startup or release restart.
python scripts/smoke_http_mcp.py --expected-version 0.1.18 --expected-mode rust-full

# Run only after explicitly enabling PP_MEMORY_SUMMARY_INDEX=1 and compact-v2.
python scripts/smoke_http_mcp.py --expected-version 0.1.18 --expected-mode rust-full --check-summary-index
```

Live release sync has a fail-closed preflight: the release repository must be
clean, on `main`, bound to the expected `origin`, and the current version tag
must be absent both locally and remotely. Validation may create runtime files,
but only the computed release paths are staged; unexpected staged, unstaged, or
untracked paths block the release. Run a dry-run first, then make the first and
only live invocation with `--push`. That live process commits, creates the
annotated tag, revalidates the pinned commit and tag object against the expected
remote state, and atomically pushes `main` plus the exact tag. Running live
without `--push` leaves a local commit and tag that make the next preflight fail.
Do not replace the attested push with a manual push or `git push --tags`.

```bash
python scripts/release-sync.py --from <base>..<merged> --audit-range <base>..<merged> \
  --version v0.1.18 --release-repo F:/Agent/plastic-promise-release \
  --expected-source-branch main \
  --expected-source-origin https://github.com/ALdaisuki/plastic-promise.git \
  --expected-origin https://github.com/ALdaisuki/plastic-promise-release.git \
  --validation-profile full --dry-run
# After the dry-run and release gates pass, repeat the same command with --push.
```

Conventions:

- Use Conventional Commits.
- Prefer small, logical PRs.
- Update documentation when behavior changes.
- Include verification notes in PRs.
- Do not merge PRs without explicit maintainer authorization.

---

## Status

| Area | Status | Notes |
|---|---|---|
| MCP server | Active | stdio and Streamable HTTP modes are implemented; legacy SSE endpoints remain available. |
| Memory pipeline | Active | Extraction, quality gate, field-scoped canonical mutations, project isolation, checked LanceDB repair jobs, feature-gated summary index writes, and decay are implemented. |
| Context supply | Active | Python remains the canonical write-side authority; governed synthesis admission is opt-in and fail-closed, while Rust snapshot recall is optional, request-scoped, and guarded at snapshot ingestion plus native-result conversion. |
| Hunter Guild | Experimental | Task lifecycle is wired; policy and scanner quality are still evolving. |
| Skills and governed workflow | Active | `session-init`, `smart-remember`, `step-closure`, and a compact 16-stage `sp-stage` governance contract are exposed; detailed skill instructions are not. |
| Extension market | Experimental | Pack validation and market commands exist; ecosystem is early. |
| Release pipeline | Active | PyPI and GitHub Actions release sync are configured. |
| Documentation | In progress | This release pass reconciles public docs with current source truth. |

---

## Documentation

| Document | Purpose |
|---|---|
| [docs/README.zh-CN.md](docs/README.zh-CN.md) | Chinese quickstart and user guide. |
| [docs/GOAL.md](docs/GOAL.md) | Chinese canonical goals, current status, and operating philosophy. |
| [docs/SYSTEM_FULL_CHAIN.md](docs/SYSTEM_FULL_CHAIN.md) | Release-facing architecture and operating chain. |
| [docs/DEVELOPER.md](docs/DEVELOPER.md) | Extension and plugin development guide. |
| [docs/architecture/architecture.md](docs/architecture/architecture.md) | Detailed architecture reference. |
| [docs/architecture/implementation-notes.md](docs/architecture/implementation-notes.md) | Practical implementation and operation notes. |
| [docs/TODO List/README.md](docs/TODO%20List/README.md) | Current unfinished roadmap items. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution workflow. |
| [SECURITY.md](SECURITY.md) | Security policy and reporting process. |

---

## License

Plastic Promise is distributed under the [MIT License](LICENSE).
