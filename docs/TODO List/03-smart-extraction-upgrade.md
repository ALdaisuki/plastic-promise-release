# 03 — Smart Extraction & Memory Lifecycle Upgrades

> Covers gaps #4, #5, #6: Real-time Tier Promotion/Demotion, Category-Aware Merge Rules, Content Chunking

## Gap #4: Real-time Tier Promotion/Demotion 🟡 P1

**Current State:** Tiers (L1/L2/L3) are set at memory creation and only changed by periodic daemon scans (`scan_tier_migration`). A memory accessed 50 times in one session stays at L1 until the next daemon cycle.

**CortexReach Approach:** Access-count-based promotion happens during retrieval:
```
Peripheral → Working: access_count > promotion_threshold (default 5)
Working → Core: access_count > higher_threshold (default 20)
Core → Working: decay_score < demotion_threshold
```

**Proposed Implementation:**

Add to `ContextEngine.increment_field()` or create a new `_maybe_promote_tier()` method:

```python
# In plastic_promise/core/context_engine.py

TIER_THRESHOLDS = {
    "L1_to_L2": 5,    # 5 accesses → promote from L1 to L2
    "L2_to_L3": 20,   # 20 accesses → promote from L2 to L3
    "L3_decay_check": 0.3,  # decay < 0.3 → demote from L3
}

def _maybe_adjust_tier(self, mid: str):
    """Check and adjust memory tier based on access count and decay."""
    mem = self._memories.get(mid)
    if not mem:
        return
    
    access_count = mem.get("access_count", 0)
    current_tier = mem.get("tier", "L1")
    decay = mem.get("decay_multiplier", 1.0)
    
    new_tier = current_tier
    
    if current_tier == "L1" and access_count >= TIER_THRESHOLDS["L1_to_L2"]:
        new_tier = "L2"
    elif current_tier == "L2" and access_count >= TIER_THRESHOLDS["L2_to_L3"]:
        new_tier = "L3"
    elif current_tier == "L3" and decay < TIER_THRESHOLDS["L3_decay_check"]:
        new_tier = "L2"
    
    if new_tier != current_tier:
        mem["tier"] = new_tier
        mem["effective_half_life"] = DECAY_CONFIG[new_tier]["half_life_days"]
        if self._sqlite:
            self._sqlite.upsert(mid, mem)
        logger.info("Tier change: %s %s → %s (access=%d, decay=%.3f)",
                    mid, current_tier, new_tier, access_count, decay)
```

**Tasks:**
1. Add `TIER_THRESHOLDS` to `constants.py`
2. Add `_maybe_adjust_tier()` to `ContextEngine`
3. Call from `_text_retrieval()` after incrementing access_count
4. Keep `scan_tier_migration` as batch backstop for missed promotions

**Expected Impact:** Frequently-used memories surface to higher tiers within a single session. Reduces reliance on daemon cycle timing.

---

## Gap #5: Category-Aware Merge Rules 🟡 P1

**Current State:** Deduplication in `smart_extractor.py` and `pipeline.py` uses a single vector similarity threshold (cos ≥ 0.85) for all categories. When two memories match, the newer one always replaces the older one.

**CortexReach Approach:** Different merge strategies per category:
- **profile/preference**: Always merge (update existing, don't create new)
- **events/cases**: Always append (never merge — each event is unique)
- **patterns**: Merge if semantically equivalent, else append
- **entities/facts**: Update if newer info, else skip

**Proposed Implementation:**

```python
# In plastic_promise/smart_extractor.py

CATEGORY_MERGE_RULES = {
    "preference": "merge_update",    # Update existing preference, don't duplicate
    "fact": "merge_if_newer",        # Replace if new info, skip if same
    "decision": "append",            # Each decision is unique, never merge
    "entity": "merge_update",        # Update entity info
    "event": "append",               # Each event is unique, never merge
    "pattern": "merge_if_similar",   # Merge if semantically equivalent (LLM check)
}

def decide_merge_action(existing_category: str, new_category: str,
                        similarity: float) -> str:
    """Return 'merge' | 'append' | 'skip' based on category rules.
    
    Cross-category matches (e.g., new 'fact' matches existing 'event'):
    default to 'append' (conservative — avoid losing unique information).
    """
    if existing_category != new_category:
        return "append"  # Different categories → don't merge
    
    rule = CATEGORY_MERGE_RULES.get(new_category, "merge_if_similar")
    
    if rule == "append":
        return "append"
    elif rule == "merge_update":
        return "merge"
    elif rule == "merge_if_newer":
        return "merge"
    elif rule == "merge_if_similar":
        # Requires LLM semantic check for patterns
        return "merge" if similarity >= 0.90 else "append"
    
    return "append"
```

**Tasks:**
1. Add `CATEGORY_MERGE_RULES` to `smart_extractor.py`
2. Add `decide_merge_action()` function
3. Update `pipeline.py` dedup logic to use category-aware rules
4. Update `check_duplicate()` in `lancedb_store.py` to return category info

**Expected Impact:** Prevents inappropriate merging (events merged into facts) and inappropriate duplication (preferences duplicated instead of updated).

---

## Gap #6: Content Chunking for Long Memories 🟡 P1

**Current State:** Memory content is embedded as a single vector regardless of length. A 2000-character memory gets one 1024-dim vector — semantic precision degrades significantly beyond ~500 characters.

**CortexReach Approach:** `chunker.ts` splits long text into overlapping chunks before embedding:
- Chunk size: ~500 characters (configurable)
- Overlap: ~100 characters (preserves cross-chunk context)
- Each chunk gets its own vector
- Retrieval returns the best-matching chunk, parent memory is returned

**Proposed Implementation:**

```python
# plastic_promise/core/chunker.py

def chunk_content(content: str, chunk_size: int = 500, overlap: int = 100) -> list[str]:
    """Split long content into overlapping chunks for embedding.
    
    Chunk boundaries respect sentence/paragraph breaks where possible.
    Returns single-element list for short content (no chunking needed).
    """
    if len(content) <= chunk_size:
        return [content]
    
    chunks = []
    start = 0
    while start < len(content):
        end = start + chunk_size
        chunk = content[start:end]
        
        # Try to break at sentence boundary
        if end < len(content):
            for sep in ['. ', '! ', '? ', '\n', '。', '！', '？']:
                last_sep = chunk.rfind(sep)
                if last_sep > chunk_size * 0.6:  # only if near the end
                    end = start + last_sep + len(sep)
                    chunk = content[start:end]
                    break
        
        chunks.append(chunk.strip())
        start = end - overlap  # overlap with previous chunk
    
    return chunks
```

**LanceDB Schema Change Required:**

Current schema stores one row per memory. With chunking, we need:
- Option A: Multiple rows per memory (one per chunk), with `chunk_index` field
- Option B: Multiple vectors per row (LanceDB supports list of vectors)

Recommend **Option A** — simpler, better search performance:

```python
# Updated LanceDB schema
_MEMORY_VECTORS_SCHEMA_V2 = pa.schema([
    pa.field("memory_id", pa.string()),
    pa.field("chunk_index", pa.int32()),       # NEW: 0-based chunk index
    pa.field("total_chunks", pa.int32()),       # NEW: total chunks for this memory
    pa.field("vector", pa.list_(pa.float32(), EMB_DIM)),
    pa.field("text", pa.string()),              # chunk text (not full memory)
    pa.field("full_text", pa.string()),         # NEW: original full content
    pa.field("tier", pa.string()),
    pa.field("category", pa.string()),
    pa.field("scope", pa.string()),
])
```

**Tasks:**
1. Create `plastic_promise/core/chunker.py`
2. Update `LanceDBStore` schema to v2 with chunk support
3. Add schema migration (v1 → v2) to `LanceDBStore._init_db()`
4. Update `insert()` to chunk before embedding
5. Update `search()` to return parent memory for any matching chunk
6. Add `PP_CHUNK_SIZE` and `PP_CHUNK_OVERLAP` env vars

**Expected Impact:** +10-20% retrieval precision for long memories (>500 chars). Storage increase proportional to content length over chunk size.

---

## Gap #8 (NEW): Memory Compaction (Progressive Summarization) 🟡 P1

**Current State:** `MemoryGC.merge_similar()` merges similar memories with cos >= 0.70 threshold. No LLM involvement, no cooldown, no age-based filtering.

**CortexReach Approach:** Sophisticated progressive summarization:
1. **Clustering**: Group memories with cosine similarity >= 0.88
2. **Age Gate**: Only compact memories older than 7 days
3. **LLM Merge**: Three-level merge preserving abstract + overview + content
4. **Cooldown**: 24h minimum between compaction runs for the same cluster
5. **Archive**: Old entries archived, not deleted (preserves history)
6. **Configurable**: `memoryCompaction.enabled`, `minAgeDays`, `similarityThreshold`, `cooldownHours`

**Proposed Implementation:**
```python
# Upgrade MemoryGC.merge_similar() in plastic_promise/memory/soul_memory.py

class MemoryCompactor:
    def compact(self, engine, min_age_days=7, similarity=0.88, cooldown_hours=24):
        # 1. Find clusters with cos >= similarity
        # 2. Filter by age >= min_age_days
        # 3. Skip clusters compacted within cooldown_hours
        # 4. For each cluster: LLM merge into single entry
        # 5. Archive old entries, insert merged entry
```

**Tasks:**
1. Add `MemoryCompactor` class to `soul_memory.py`
2. Add compaction cooldown tracking (SQLite `compaction_history` table)
3. Add LLM merge prompt for three-level memory compaction
4. Add `PP_COMPACTION_ENABLED` env var (default off until validated)
5. Wire into `memory_gc` flow (compaction happens before decay cleanup)

---

## Gap #9 (NEW): Extraction Throttling 🟡 P1

**Current State:** Smart extractor has a per-call cap of 3 LLM calls total. No rate limiting across calls — rapid-fire conversations could trigger unlimited extractions.

**CortexReach Approach:** Sliding one-hour window rate limiter:
- Default `maxExtractionsPerHour` = 30
- Optional `skipLowValue` flag to skip low-importance extractions when near limit
- Configurable window size and max

**Proposed Implementation:**
```python
# In plastic_promise/smart_extractor.py

class ExtractionThrottle:
    def __init__(self, max_per_hour=30):
        self._timestamps = []  # sliding window
    
    def allow(self) -> bool:
        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 3600]
        return len(self._timestamps) < self._max_per_hour
    
    def record(self):
        self._timestamps.append(time.time())
```

**Tasks:**
1. Add `ExtractionThrottle` class to `smart_extractor.py`
2. Check throttle before LLM fallback calls
3. Add `PP_EXTRACTION_MAX_PER_HOUR` env var

---

## Updated CortexReach Merge Decisions (correction)

> **Agent 2 更正**: CortexReach 的 LLM 去重决策有 7 种类型，而非最初认为的简单 MERGE/SKIP：

| Decision | Meaning | Plastic Promise Equivalent |
|----------|---------|---------------------------|
| CREATE | New memory, no match found | Default insert |
| MERGE | Combine with existing | Vector similarity merge |
| SKIP | Duplicate, don't store | Quality gate rejection |
| SUPERSEDE | New info replaces old | Update existing |
| SUPPORT | Corroborating evidence, strengthen existing | worth_success++ |
| CONTEXTUALIZE | Add context to existing | Append to l2_content |
| CONTRADICT | Contradicts existing, store both with cross-reference | Not implemented |

Plastic Promise currently only has CREATE/MERGE/SKIP. SUPPORT and CONTEXTUALIZE would be valuable additions to the worth_score system.
