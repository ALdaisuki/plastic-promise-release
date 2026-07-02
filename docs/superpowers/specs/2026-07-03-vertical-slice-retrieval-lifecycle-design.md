# 8-Unit Vertical Slice — Design Spec

**Date**: 2026-07-03
**Status**: Design — pending review
**Scope**: Integrate CortexReach retrieval patterns into Plastic Promise governance architecture

## Problem

Plastic Promise's memory retrieval pipeline has 4 structural gaps and 3 dead/incomplete code paths that degrade recall quality:

1. **No query expansion** — short/ambiguous queries miss relevant memories
2. **Fragmented reranker** — two separate Ollama-only implementations, opt-in, different models
3. **Decay computed but unused** — `decay_multiplier` stored but never affects retrieval ranking
4. **Vector MMR stubbed** — diversity stage uses zero-vector dummy, never actually deduplicates
5. **L2 ghost tier** — DECAY_CONFIG has L2 but MEMORY_TIERS and classify_tier skip it
6. **update_all_decay() + evolve_cycle() dead code** — complete implementations with zero callers
7. **Rust engine on stale worktree** — 4 commits behind main, `:memory:` bug, formula divergences

These gaps mean retrieval quality is lower than it should be, stored lifecycle data is wasted, and the Rust acceleration path is blocked.

## Architecture

8 independent units across 3 layers, each env-var gated, zero new MCP tools:

```
Retrieval Layer (supply pipeline)
  Unit 1: Query expansion → injects synonyms before BM25
  Unit 2: Unified reranker → 4-provider chain replaces 2 fragmented impls
  Unit 3: Decay ranking → stored decay data finally used in scoring

Storage Layer (memory lifecycle)
  Unit 4: L2 tier → classifies memories into 3 tiers instead of 2
  Unit 5: Periodic cron → activates dead maintenance code
  Unit 6: Vector MMR → fixes stubbed diversity dedup

Infrastructure Layer
  Unit 7: Emoji detection → prevents emoji-only pollution
  Unit 0: Rust merge → rebase worktree, fix formulas, inject graph
```

Each unit gates behind an env var (default ON for P0/P1). Rollback is per-unit: set env var to 0.

## Components

### Unit 1: Query Expansion (`query_expander.py` NEW)

**What**: Local synonym dictionary, zero API calls. Chinese substring match + English `\b` regex. Domain-aware filtering. Max 3 expansion terms.

**Why**: BM25 text search benefits from synonym expansion. Vector search does not — semantic models handle synonyms natively.

**Interface**: `expand_query(query: str, domain_hint: str = None) -> str`

**Insertion**: `context_engine.py:1290`, before `_text_retrieval()` call. Vector search unaffected.

**Env**: `PP_QUERY_EXPANSION=1` (default on)

### Unit 2: Unified Multi-Provider Reranker (`reranker.py` REWRITE)

**What**: Delete inline `_apply_rerank()` (context_engine.py:1184-1246). Upgrade `reranker.py` to `MultiProviderReranker` class with 4-provider chain: Jina → SiliconFlow → Ollama → cosine. Always-on.

**Why**: Two separate implementations with different models and timeout strategies cause inconsistent behavior. Multi-provider chain ensures rerank survives any single provider failure.

**Interface**: `MultiProviderReranker().rerank(query: str, candidates: list) -> list`

**Blend**: `final = 0.6*ce + 0.4*original`, floor `original*0.5`

**Free tier limits**: Jina 1M tokens/day, SiliconFlow 1K calls/day. Exceeded → auto-degrade to next provider (not an error).

**Env**: `PP_RERANK_DISABLED=1` (emergency off), `PP_RERANK_PROVIDERS=jina,siliconflow,ollama,cosine`

### Unit 3: Decay-Aware Ranking (`context_engine.py` MODIFY)

**What**: Two-formula system applied in supply() loop:
- **A (additive)**: `score = clamp01(score + exp(-age/recency_hl)*0.1, score)` — fresh bonus
- **B (multiplicative)**: `score = clamp01(score * factor, score*0.5)` where `factor = 0.5 + 0.5*exp(-age/effectiveHL)` — time penalty

**Trust modulation**: `recency_hl = 14.0 * (1.0 + (trust_boost - 1.0)*0.5)` — high-trust agents get wider freshness window

**Why**: `decay_multiplier` and `effective_half_life` are computed and stored but never affect retrieval ranking. This activates them.

**Insertion**: `context_engine.py:1337`, after feedback multiplier, before length norm. Pure compute, zero I/O.

**Env**: `PP_DECAY_IN_RANKING=1` (default on)

### Unit 4: L2 Tier Completion (`constants.py` + `soul_memory.py` MODIFY)

**What**: Add L2 to MEMORY_TIERS with capacity/ttl/threshold. Insert intermediate branch in classify_tier(). Change promote/demote to stepwise (L1↔L2↔L3). Real-time promotion during retrieval access_count increment.

**Why**: DECAY_CONFIG has L1/L2/L3 with decay parameters, but MEMORY_TIERS only has L1/L3. 123 L2 memories were set by daemon SQL migration, not by MemoryTierManager. classify_tier() outputs only L1 or L3.

**Thresholds**: `L1→L2: composite>=0.4 AND access>=5 | L2→L3: composite>=0.7 AND access>=20`

**Env**: `PP_TIER_AUTO_PROMOTE=1` (default on)

### Unit 5: Periodic Cron Activation (`scan_memory_decay.py` MODIFY)

**What**: Add routine maintenance calls to existing scan_memory_decay(). Activate `update_all_decay()` (zero callers) and `evolve_cycle()` (non-periodic). Add 4th detection dimension: decay anomaly (decay<0.2 AND access>10) → `fix_memory` task → Hunter Guild.

**Why**: Complete lifecycle code exists but never runs automatically. Decay values drift without periodic recalculation.

**Order**: Routine maintenance BEFORE anomaly detection (use fresh decay values).

**Env**: `PP_PERIODIC_MAINTENANCE=1` (default on)

### Unit 6: Vector MMR Fix (`lancedb_store.py` + `context_engine.py` MODIFY)

**What**: Add `LanceDBStore.get_vector(memory_id)` method. Replace zero-vector dummy in `_apply_mmr()` Stage 2 with real vector lookup. Pre-build vec_cache per supply() call. Compare against last 5 selected items.

**Why**: Stage 2 of `_apply_mmr()` calls `search_similar([0.0]*1024, k=1)` — a zero-vector dummy that never matches anything. All items pass through without diversity filtering.

**Thresholds**: Cosine > 0.85 → demote (penalty 0.70). Not removed, deferred to end.

**Env**: `PP_MMR_VECTOR=1` (default on)

### Unit 7: Emoji Detection (`noise_filter.py` MODIFY)

**What**: Use Python `emoji` library for Unicode emoji detection. Check before length gate. ~10 lines.

**Why**: Emoji-only messages pass through unfiltered into the memory pool. Wastes storage and retrieval bandwidth. `emoji` library handles full Unicode emoji spectrum (ZWJ sequences, skin tones, flags) without manual regex maintenance.

### Unit 0: Rust Engine Merge (`rust/` + `context_engine.py`)

**What**: Rebase `worktree-rust-engine-phase2` onto main. Fix 3 formula divergences (RRF K 60→20, decay linear→log1p, MMR Jaccard→cosine+Jaccard). Inject graph traversal: serialize `_graph_nodes`+`_graph_edges` to JSON, call `rust.load_graph()`. ~10 lines Python.

**Why**: 4 Rust commits fix `:memory:` bug and schema alignment. Stale worktree blocks Rust acceleration path.

**Verification**: `PP_FORCE_PYTHON_SUPPLY=0` → Rust path returns principles>=2, graph_nodes>=50.

## Data Flow

```
User Query
  → Unit 1: expand_query() → expanded query
  → _text_retrieval(expanded_query) → BM25 results
  → _vector_retrieval(task_vector) → LanceDB ANN results
  → _hybrid_fuse() → fused results
  → Unit 3: _apply_decay_awareness() → decay-adjusted scores
  → Unit 2: MultiProviderReranker.rerank() → reranked results
  → Unit 6: _apply_mmr() → diversity-filtered results
  → Layer assignment → ContextPack

Periodic (Unit 5):
  scan_memory_decay() → update_all_decay() → evolve_cycle()
    → Unit 4: real-time tier promotion during retrieval access_count++
    → decay anomaly detection → fix_memory → Hunter Guild
```

## Error Handling

- Unit 1: Expansion failure → pass original query unchanged
- Unit 2: All providers down → cosine fallback (always available)
- Unit 3: Missing decay data → skip decay adjustment (score unchanged)
- Unit 5: Maintenance failure → log warning, scan continues
- Unit 6: Vector unavailable → fall back to content-only MMR (Stage 1)
- Unit 7: Regex compilation failure → skip emoji check
- Unit 0: Rust unavailable → Python fallback (existing behavior)

All external calls (Jina, SiliconFlow) wrapped in try/except with 5s timeout. Never block retrieval.

## Testing

### Per-Unit Verification
```bash
# Unit 1: expand_query("挂了") contains "crash"
# Unit 2: Stop Ollama → Jina auto-takes over
# Unit 3: Two similar memories 30 days apart → newer ranks higher  
# Unit 4: classify_tier() returns "L2" for composite=0.5, access=10
# Unit 5: scan_memory_decay() calls update_all_decay() (check logs)
# Unit 6: "memory system" query → no 3 near-identical results
# Unit 7: is_noise("👍") == True
# Unit 0: PP_FORCE_PYTHON_SUPPLY=0 → principles >= 2
```

### Regression
```bash
python -m pytest tests/ -x -q --tb=short
# All existing tests must pass after each unit
```

## Constraints

- No new MCP tools — pure internal improvements
- No LanceDB schema migration — schema v1 unchanged
- No API changes — `supply()`, `memory_store()`, `memory_recall()` signatures unchanged
- Per-unit env var gate — independent rollback
- Governance atoms (defense, step_closure) run automatically via PR #11 injection
- Trust-score modulation reuses existing `trust_boost` variable
- All external providers use free tiers (no billing dependency)
