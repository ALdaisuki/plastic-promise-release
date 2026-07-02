# 01 — Full Architectural Comparison: Plastic Promise vs CortexReach memory-lancedb-pro

> Comparative analysis date: 2026-07-03
> CortexReach version: v1.1.0-beta.10 (37 releases, 4.4k stars, 728 forks, MIT)

## 1. Project Positioning

| Dimension | Plastic Promise | CortexReach memory-lancedb-pro |
|-----------|----------------|-------------------------------|
| **Scope** | Full AI behavior governance system | Memory plugin for OpenClaw agents |
| **Paradigm** | Commitment Engineering (12 principles) | Plugin architecture (hooks + tools) |
| **Language** | Python + Rust (PyO3) | TypeScript + JavaScript |
| **Distribution** | MCP server (48 tools) | npm package (`memory-lancedb-pro`) |
| **Database** | SQLite (write-through) + LanceDB (vectors) | LanceDB (vectors + FTS) |
| **Embedding** | Ollama mxbai-embed-large (0.7GB) | OpenAI-compatible API abstraction |
| **Ecosystem** | Internal (single project) | Community (11 translations, one-click setup) |
| **Agent Model** | Multi-agent: Claude PM + Pi Builder/Fixer/Reviewer | Single agent with memory capability |
| **Agent Tools** | 48 MCP tools (11 domains) | 18 contracted tools (memory_recall, memory_search, memory_fact_query, memory_store, memory_update, memory_forget, memory_list, memory_stats, memory_debug, memory_compact, memory_archive, memory_promote, memory_explain_rank, memory_reflection_resolve + 4 self_improvement tools) |

## 2. Retrieval Pipeline Comparison

### CortexReach Pipeline (13 stages, full trace)
```
Query → Adaptive Skip Gate (before embedding!)
      → Query Expansion (local synonym dict, zero API)
      → Embed Query
      → Vector ANN (cosine) + BM25 FTS (parallel)
      → Score Fusion: vectorScore + bm25Hit * 0.15 * vectorScore
      → Min Score Filter (0.3)
      → Cross-Encoder Rerank (60% CE + 40% fused, floor fused*0.5)
      → Additive Recency Boost: clamp01(score + exp(-age/14)*0.1, score)
      → Importance Weighting: 0.7 + 0.3*importance
      → Length Normalization (anchor: 500)
      → Multiplicative Time Decay (with access-reinforced half-life)
      → Hard Min Score (0.35)
      → Noise Filter → MMR Diversity → Final Top-K Setwise Selection
```

### Plastic Promise Pipeline (9 stages)
```
Query → Graph Traversal (principle↔memory edges)
      → BM25 Text (Okapi, k1=1.2, b=0.75)
      → LanceDB Vector ANN (cosine)
      → Hybrid Fusion (weighted combination)
      → Symbol Rules Boost (security ×1.5, commitment ×1.4, quality ×1.2)
      → Feedback Multiplier (worth-based)
      → Length Normalization (anchor: 500)
      → [Optional] Rerank (PP_RECALL_RERANK=1, Ollama only)
      → MMR Diversity (content-based dedup, vector MMR stubbed)
      → Layer Assignment (core ≥ 0.60, related ≥ 0.35, divergent ≥ 0.15)
```

### Key Differences

| Stage | Plastic Promise | CortexReach | Gap |
|-------|----------------|-------------|-----|
| Pre-Retrieval Gate | ❌ None | ✅ Adaptive skip/force before embedding | **New gap** |
| Graph Traversal | ✅ Unique advantage | ❌ None | — |
| BM25 | Custom Okapi, no FTS index | LanceDB native FTS, sigmoid norm | CortexReach faster at scale |
| Query Expansion | ❌ None | ✅ Local synonym dict, no API | **Gap #1** |
| Score Fusion | Weighted combination | BM25 as 15% bonus on vector | CortexReach simpler |
| Reranker | Opt-in, Ollama only, generate API | Always-on, 5 providers, dedicated API | **Gap #2** |
| Recency Boost | ❌ Not implemented | Additive exp(-age/14)*0.1 bonus | **Gap #3** |
| Time Decay | Not in retrieval | Multiplicative with access-reinforced HL | **Gap #3** |
| Length Norm | Same formula (anchor 500) | Same formula (anchor 500) | Equivalent |
| Min Score | Layer thresholds (0.60/0.35/0.15) | Hard cutoff + per-stage floors | Plastic Promise more nuanced |
| MMR | Content-only (first 200 chars) | Cosine similarity + setwise diversity | **Gap #7** |
| Pipeline Trace | ❌ None | ScoreHistory per stage, RetrievalTrace | **New gap** |

## 3. Memory Lifecycle Comparison

### Decay Engine

> **Agent 2 更正**: CortexReach 的 `decay-engine.ts` 并非独立文件——衰减逻辑分布在 `retriever.ts`（三种指数公式）、`access-tracker.ts`（间隔重复）和配置 schema（43 个配置对象）中。

Both use the identical Weibull formula, but CortexReach uses **three distinct exponential formulas**:

| Formula | Type | CortexReach | Plastic Promise |
|---------|------|-------------|-----------------|
| Time Decay | Multiplicative | `factor = 0.5 + 0.5*exp(-age/effectiveHL)` | `exp(-lambda * days^beta)` |
| Recency Boost | Additive | `boost = exp(-age/hl) * weight` added to score | Not implemented |
| DecayEngine Path | Delegated | Full composite when DecayEngine active | Not implemented |
| Access Reinforcement | Logarithmic | `extension = baseHL * rf * log1p(effectiveAccess)` | Same formula (log1p) |

**Key difference**: CortexReach's recency boost is ADDITIVE (adds to score) while Plastic Promise's decay is purely multiplicative (scales score down). Additive recency means recently-created memories get a score bonus independent of their vector/text relevance.

### Tier Management

| Feature | Plastic Promise | CortexReach |
|---------|----------------|-------------|
| Tier Count | 3 (L1/L2/L3) | 3 (Peripheral/Working/Core) |
| Promotion Trigger | Daemon scan (periodic) | Access count threshold (real-time) |
| Demotion Trigger | Daemon scan (periodic) | Composite score + age threshold (real-time) |
| Configurable Thresholds | ❌ Hard-coded in scan_tier_migration | ✅ 43 config objects, 117 UI hints |
| Per-Tier Beta | L1=1.5, L2=1.2, L3=0.7 | Core=0.8, Working=1.0, Peripheral=1.3 |
| Per-Tier Decay Floor | Not implemented | Core=0.9, Working=0.7, Peripheral=0.5 |

### Additional Features (CortexReach only)

| Feature | Description | Plastic Promise Status |
|---------|-------------|----------------------|
| **Memory Compaction** | Clustering similar memories (cos >= 0.88, >7 days old), LLM merging with cooldown | Not implemented |
| **Obsidian Vault Sync** | Export as markdown with YAML frontmatter, category→folder mapping | Not implemented |
| **Extraction Throttling** | Sliding 1-hour window rate limiter (default 30/hr) | Not implemented |
| **Dreaming/Reflection** | Cron-driven light/deep/REM phases, separate model config per phase | Partial (step-closure reflection, no scheduled phases) |
| **Debounced Access Tracking** | 5s debounce with Map buffering, batch flush, auto-requeue on failure | Simple access_count increment |
| **Round-Robin Key Rotation** | API key arrays with automatic rotation on rate-limit errors | Not implemented |

### Deduplication

| Stage | Plastic Promise | CortexReach |
|-------|----------------|-------------|
| Vector Similarity | ✅ cos ≥ 0.85 | ✅ cos ≥ 0.7 (pre-filter) |
| Semantic Merge | ❌ Not implemented | ✅ LLM semantic merge decision |
| Category-Aware Rules | ❌ Not implemented | ✅ profile=merge, events=append |

## 4. Smart Extraction Comparison

| Feature | Plastic Promise | CortexReach |
|---------|----------------|-------------|
| Categories | 6 (preference/fact/decision/entity/event/pattern) | 6 (profile/preferences/entities/events/cases/patterns) |
| Primary Method | Rule-based (keyword matching) | LLM-based (configurable model) |
| LLM Fallback | Ollama, capped at 3 calls total | Primary path, per-turn |
| L0/L1/L2 Layers | ✅ Three layers | ✅ Three layers |
| L0 Generation | Regex split (first sentence) | LLM one-sentence summary |
| L1 Generation | `[{category}] {text[:300]}` | LLM structured summary |
| Cache | SHA-256 hash, LRU 128, TTL 300s | Not detailed |
| Per-Turn Cap | 3 LLM calls total | Up to 3 memories per turn |

## 5. Adaptive Retrieval Comparison

| Feature | Plastic Promise | CortexReach |
|---------|----------------|-------------|
| Skip Patterns | ✅ Regex + greetings + affirmations | ✅ Regex + commands + system |
| Force Patterns | ✅ Memory + task keywords | ✅ Memory intent keywords |
| CJK Support | ✅ Threshold: 4 chars | ✅ Threshold: 6 chars |
| Question Override | ✅ Always retrieve | ✅ Always retrieve |
| Normalization | ❌ None | ✅ Strips OpenClaw metadata, cron wrappers, timestamps |
| Task Keywords | ✅ 50+ engineering keywords | ❌ None |
| Control Patterns | ❌ None | ✅ Session control detection |

## 5.5 Reflection Subsystem (CortexReach Deep-Dive)

> **Agent 1 深挖**: CortexReach 的 reflection 子系统（13 个文件）是其最复杂的架构组件。Plastic Promise 的 step-closure 是行为层面的，CortexReach 的 reflection 是检索质量层面的——两者互补。

### Storage Types (4 kinds with different decay)

| Kind | Midpoint (days) | k | BaseWeight | Quality |
|------|-----------------|---|------------|---------|
| decision | 45 | 0.25 | 1.1 | 1.0 |
| user-model | 21 | 0.3 | 1.0 | 0.95 |
| agent-model | 10 | 0.35 | 0.95 | 0.93 |
| lesson | 7 | 0.45 | 0.9 | 0.9 |
| invariant (stable rules) | 45 | 0.22 | 1.1 | 1.0 |
| derived (per-session) | 7 | 0.65 | 1.0 | 0.95 |

### Logistic Decay Formula
```
score(t) = 1/(1+exp(k*(ageDays-midpoint))) * baseWeight * quality * fallbackFactor
```
Sharper cutoff than exponential Weibull — better for time-boxed knowledge where relevance drops abruptly.

### Aggregation Multi-Factor Score
```
finalScore = 0.50*baseScore + 0.16*supportScore + 0.12*freshnessScore 
           + 0.16*stabilityScore + 0.06*qualityScore
```
Where `supportScore = 1-exp(-repeatCount/2.5)` (diminishing returns) and `stabilityScore` penalizes burst clusters within 6-hour windows.

### Plastic Promise Counterpart
Plastic Promise's `step-closure` six-chain loop reflects on agent behavior (principles→SCARF→hormones→trust→reflection→CEI). CortexReach's reflection reflects on knowledge quality (invariants vs derived, logistic decay, aggregation scoring). These are complementary — Plastic Promise could add knowledge-quality reflection without changing its behavioral reflection.

---

## 6. Key Architectural Decisions (from CortexReach Deep Research)

### From CortexReach → Plastic Promise

1. **Pre-Retrieval Adaptive Gate** — Skip/force detection BEFORE embedding saves API calls on greetings, commands, system messages. CortexReach gates before `embed_query()`.

2. **BM25-as-Bonus Fusion** — Score-level fusion: `vectorScore + bm25Hit * 0.15 * vectorScore`. Simpler than weighted combination, BM25 acts as confidence boost.

3. **Additive Recency Boost** — `clamp01(score + exp(-age/14)*0.1, score)`. Recent memories get a flat bonus independent of relevance, surfacing fresh context.

4. **Pipeline Trace (ScoreHistory)** — Each result carries per-stage score deltas. Complete observability into why a result surfaced.

5. **Multi-Provider Reranker Adapters** — Jina, SiliconFlow (free tier), Voyage, Pinecone, vLLM. Unified request/response parsing with cosine fallback.

6. **Query Expansion as Local Synonym Dict** — Zero API calls. CJK triggers by substring, English by word boundary. Max 5 expansion terms.

7. **Real-Time Tier Changes** — Composite score thresholds (0.4 for Working, 0.7 for Core) applied during retrieval, not daemon cycle.

8. **Three-Tier Reflection Storage** — Event (envelope) / Invariant (45d half-life, stable rules) / Derived (7d half-life, session-specific). Different decay rates per knowledge type.

9. **Logistic Decay for Reflection** — `score(t) = 1/(1+exp(k*(ageDays-midpoint))) * baseWeight * quality`. Sharper cutoff than exponential, better for time-boxed knowledge.

10. **Greedy Setwise Diversity Selection** — Reusable module with Jaccard + cosine penalties at multiple thresholds. Used by both auto-recall and reflection recall.

11. **Dual Normalization Keys** — Strict (exact matching) and soft (punctuation-stripped, for fuzzy similarity). Enables both precise dedup and diversity detection.

12. **Fixture-Based Benchmarking** — Three tiers (smoke/baseline/gate), six expectation types, per-result score trails. CI-gated regression testing on retrieval quality.

### From Plastic Promise → CortexReach (what they could learn)

1. **Principle-Aligned Retrieval** — Retrieval results filtered/boosted by governance principles.
2. **Entity Graph Traversal** — Multi-hop principle↔memory traversal for deeper context.
3. **Trust-Weighted Retrieval** — Retrieval scope adapts to agent trust score.
4. **Domain Federation** — Automatic merging of related domains with weighted signals.
5. **Step Closure** — Post-task reflection generating structured improvement memories.
6. **Hunter Guild Delegation** — Task routing based on agent capability and trust.

## 7. Numerical Comparison

| Metric | Plastic Promise | CortexReach |
|--------|----------------|-------------|
| GitHub Stars | — | 4,400 |
| Releases | — | 37 |
| npm Downloads | — | Not disclosed |
| MCP Tools / Agent Tools | 48 | 4 (+5 optional) |
| Code Files | ~80 Python | ~30 TypeScript |
| Lines of Code (core) | ~2,700 (context_engine) | ~500 (retriever) |
| Database Tables | 6+ (SQLite) + 1 (LanceDB) | 1 (LanceDB) |
| Embedding Dim | 1024 (mxbai) | Configurable |
| Retrieval Latency | <200ms target | Not disclosed |
| Memory Pool | ~200 records | Configurable |
| Supported Languages | Python, Rust via PyO3 | TypeScript, JavaScript |
