# 02 — Retrieval Pipeline Enhancements

> Covers gaps #1, #2, #3, #7: Query Expansion, Multi-Provider Reranker, Decay-in-Ranking, Vector MMR

## Gap #1: Query Expansion — Local Synonym Dictionary 🔴 P0

**Current State:** Plastic Promise has no query expansion. Short or ambiguous queries (e.g., "fix the bug") rely entirely on BM25 keyword matching and vector similarity.

**CortexReach Approach (Agent 1 深挖更正):** `query-expander.ts` is a **lightweight, zero-API-call synonym dictionary** — NOT LLM-based:
- Chinese triggers: exact substring match (no word boundaries in Chinese)
- English triggers: word-boundary regex (`\b`)
- Max 5 expansion terms appended to query
- Already-present terms skipped (idempotent)
- Short queries (<2 chars) pass through unchanged
- Example: "挂了"/"炸了" → expands to "崩溃 crash error 报错"
- Used only for BM25 search — vector search gets the raw query

**Proposed Implementation:**

```python
# plastic_promise/core/query_expander.py

def expand_query(query: str, ollama_host: str = None, max_expansions: int = 3) -> list[str]:
    """Expand a user query into multiple search variants.
    
    Strategy (no LLM required by default):
    1. Extract key noun phrases (rule-based)
    2. Generate synonym variants from domain vocabulary
    3. Add Chinese-English bilingual variants if CJK detected
    4. LLM-based expansion as optional fallback for ambiguous queries
    
    Returns original query + up to max_expansions variants.
    """
```

**Tasks:**
1. Create `plastic_promise/core/query_expander.py` (~150 lines)
2. Integrate into `ContextEngine._supply_python()` before text/vector retrieval
3. Run each expansion variant through retrieval, merge + dedup results
4. Add `PP_QUERY_EXPANSION=1` env var gate (default on)

**Expected Impact:** +15-25% recall for short/ambiguous queries. Negligible latency increase (expansions run in parallel).

---

## Gap #2: Cross-Encoder Reranker Upgrade 🔴 P0

**Current State:** Reranker exists but is:
- Opt-in only (`PP_RECALL_RERANK=1`)
- Ollama-only provider
- Uses `/api/generate` (not dedicated rerank API)
- Not integrated into default retrieval pipeline

**CortexReach Approach:** Always-on multi-provider reranker:
- Jina AI (`jina-reranker-v2`): Free tier, 1M tokens/day
- SiliconFlow: Free tier, dedicated rerank endpoint
- Voyage AI: `rerank-2` model
- Pinecone: Managed rerank
- Graceful degradation: falls back to cosine similarity on all providers down

**Proposed Implementation:**

```python
# plastic_promise/core/reranker.py — upgrade

class MultiProviderReranker:
    """Multi-provider cross-encoder reranker with fallback chain.
    
    Provider priority (configurable):
    1. Jina AI (free tier, fastest) — POST https://api.jina.ai/v1/rerank
    2. SiliconFlow (free tier) — POST https://api.siliconflow.cn/v1/rerank
    3. Ollama local (no network) — generate API with structured prompt
    4. Cosine similarity fallback (always available)
    
    Blend: 60% cross-encoder + 40% original score
    Timeout: 5s per provider, total 10s across all attempts
    """
```

**Tasks:**
1. Upgrade `plastic_promise/core/reranker.py` with provider abstraction
2. Add Jina AI provider (free tier, no API key swap needed for basic usage)
3. Add SiliconFlow provider (free tier)
4. Keep Ollama as fallback
5. Add cosine similarity as final fallback
6. Change default to always-on (`PP_RECALL_RERANK=1` → default)
7. Add `PP_RERANK_PROVIDERS` env var for provider ordering
8. Update `_supply_python()` to call reranker unconditionally

**Expected Impact:** +10-20% relevance precision. 5-10s added latency (acceptable for quality gain). Zero cost (all free tiers).

---

## Gap #3: Decay Score in Retrieval Ranking (Two-Formula System) 🔴 P0

**Current State:** `decay_multiplier` and `effective_half_life` are computed and stored, but NEVER used in retrieval relevance scoring.

**CortexReach Approach (Agent 1 深挖更正):** TWO distinct formulas, not one:

**Formula A — Additive Recency Boost** (makes new memories outrank old ones):
```
boost = exp(-ageDays / 14) * 0.1
newScore = clamp01(score + boost, floor=score)
```
This is ADDITIVE — brand-new memories get up to +0.1 regardless of their vector/text relevance.

**Formula B — Multiplicative Time Decay** (penalizes old memories):
```
factor = 0.5 + 0.5 * exp(-ageDays / effectiveHalfLife)
newScore = clamp01(score * factor, floor=score * 0.5)
```
`effectiveHalfLife` is extended by access reinforcement (log1p formula). Floor at 0.5x prevents total disappearance.

**Key insight**: Additive recency means CortexReach actively BOOSTS new content. Plastic Promise only multiplies (scale down old). The additive path is what surfaces fresh memories that might otherwise score low on vector similarity.

**Proposed Implementation:**

```python
def _apply_decay_boost(self, score, mem, current_time_str):
    """Two-formula decay-aware relevance adjustment."""
    if not mem:
        return score
    
    created_at = mem.get("created_at", current_time_str)
    age_days = self._days_since(created_at, current_time_str)
    
    # Formula A: Additive recency boost
    recency_boost = math.exp(-age_days / 14.0) * 0.1
    score = min(1.0, score + recency_boost)
    
    # Formula B: Multiplicative time decay with access reinforcement
    effective_hl = mem.get("effective_half_life", 60.0)
    factor = 0.5 + 0.5 * math.exp(-age_days / effective_hl)
    score = max(score * 0.5, score * factor)  # floor at 0.5x
    
    return score
```

**Tasks:**
1. Add `_apply_decay_boost()` method to ContextEngine
2. Call it during the single-pass item construction loop
3. Add `PP_DECAY_IN_RANKING=1` env var (default on)

**Expected Impact:** Fresher, more frequently used memories rank higher. Decayed memories naturally sink. Zero added latency (all data already in memory dict).

---

## Gap #7: Vector-Based MMR Diversity 🟡 P1

**Current State:** MMR in `_apply_mmr()` only does content-based dedup (first 200 chars exact match). The vector-based MMR path is stubbed out — it calls `self._ldb.search_similar()` with a dummy zero vector.

**CortexReach Approach:** Full cosine similarity MMR:
```
for each candidate in sorted by score:
    max_sim = max(cosine_similarity(candidate, selected) for selected in already_chosen)
    if max_sim > 0.85:
        candidate.score *= 0.70  // demote
        deferred.append(candidate)
    else:
        selected.append(candidate)
```

**Proposed Implementation:**

Fix the stubbed vector MMR path:

```python
def _apply_mmr(self, items, threshold=0.85, penalty=0.70):
    # ... existing content dedup ...
    
    # Stage 2: Vector-based MMR
    if self._ldb and len(selected) > 0:
        for item in items_sorted:
            if item.is_principle:
                continue
            # Get stored vector from LanceDB
            item_vec = self._ldb.get_vector(item.id)
            if item_vec is None:
                selected.append(item)
                continue
            
            # Check max similarity against already-selected items
            max_sim = 0.0
            for sel in selected:
                sel_vec = self._ldb.get_vector(sel.id)
                if sel_vec:
                    sim = self._cosine_similarity(item_vec, sel_vec)
                    max_sim = max(max_sim, sim)
            
            if max_sim > threshold:
                item.relevance *= penalty
                deferred.append(item)
            else:
                selected.append(item)
```

This requires adding a `get_vector(memory_id)` method to `LanceDBStore`.

**Tasks:**
1. Add `LanceDBStore.get_vector(memory_id)` method
2. Implement real vector MMR in `_apply_mmr()`
3. Cache retrieved vectors during single supply() call (avoid repeated LanceDB lookups)

**Expected Impact:** Better result diversity. Prevents near-duplicate memories from crowding out diverse results.

---

## Bonus Gap: Pipeline Trace (ScoreHistory + RetrievalTrace) 🟢 P2

**Current State:** No observability into why a result surfaced or how its score evolved through the pipeline stages.

**CortexReach Approach:** Every `retrieveWithTrace()` call produces a `RetrievalTrace` with:
- Per-stage input/output counts and elapsed time
- Each result carries optional `ScoreHistory` (array of `ScoreStep`: stage name, score before/after, delta)
- Telemetry aggregates: total/skipped/zero-result, latency, source breakdowns

**Proposed Implementation:**
```python
@dataclass
class ScoreStep:
    stage: str
    score_before: float
    score_after: float
    delta: float

# ContextItem gets optional score_history field
# ContextPack.audit_metadata gets trace_summary
```

**Tasks:**
1. Add `ScoreStep` dataclass to context_engine.py
2. Add optional `score_history: list[ScoreStep]` to ContextItem
3. Instrument each pipeline stage to record score deltas
4. Add `trace_summary` to audit_metadata
5. Gate behind `PP_TRACE_SCORES=1` (off by default — overhead)

---

## Implementation Order

All three P0 items are independent — can be done in parallel:

```
Task 1: Query Expansion (~150 lines, 2 hours)
  → new file: plastic_promise/core/query_expander.py
  → integrate into _supply_python()

Task 2: Reranker Upgrade (~200 lines, 3 hours)
  → modify: plastic_promise/core/reranker.py
  → new providers: Jina, SiliconFlow

Task 3: Decay-in-Ranking (~50 lines, 1 hour)
  → modify: plastic_promise/core/context_engine.py
  → add _apply_decay_boost(), call in supply loop

Task 4: Vector MMR (~100 lines, 2 hours)
  → modify: plastic_promise/core/lancedb_store.py (add get_vector)
  → modify: plastic_promise/core/context_engine.py (fix _apply_mmr)
```
