# Direction B: Memory Quality Pipeline — Design Spec

> **Date**: 2026-06-30
> **Status**: Design approved, awaiting implementation plan
> **Depends on**: Direction A (Weibull decay + access reinforcement + composite scoring)
> **Related**: [LanceDB vector storage](2026-06-30-lancedb-vector-dashboard-design.md), [Memory lifecycle decay](2026-06-30-memory-lifecycle-decay-design.md)

## 1. Problem Statement

`smart_extractor.py` implements 6-category extraction + L0/L1/L2 layering + LLM fallback, but it is not connected to `pipeline.py`. Additionally, three quality mechanisms are missing:

1. **No dedup** — near-identical memories are stored as separate records
2. **No entry gating** — low-quality or irrelevant content enters the pool unconditionally
3. **No merge** — similar memories accumulate without consolidation, bloating the pool

## 2. Architecture Overview

Modified pipeline data flow (★ = new):

```
store_urgent()
  │
  ├─ ★ smart_extractor.extract_memories()   (Task 1: pre-extraction)
  │    └─ Returns: [ExtractedMemory] with category, L0/L1/L2, confidence
  │
  ▼
raw → tagged → classified → embedded → ★ migrate (enhanced)
                                            │
                                            ├─ ★ Vector dedup (Task 2)
                                            │    └─ LanceDB.search() top-1, cos ≥0.85 → bump counters
                                            │
                                            ├─ ★ QualityGate.score() (Task 3)
                                            │    └─ 4-dim × 0.25: ≥0.5 store | 0.3-0.5 low_quality | <0.3 discard
                                            │
                                            └─ RecMem.store() → LanceDB

MemoryGC.collect() (existing cycle, ~7 days)
  │
  ├─ mark_decaying()    (existing)
  ├─ ★ merge_similar()  (Task 4)
  │    └─ LanceDB ANN full-pool scan, cos ≥0.70 → keep best, merge summaries
  └─ forget()           (existing, cleans decayed + merged records)
```

### Files Changed

| File | Change | Task |
|------|--------|------|
| `plastic_promise/memory/pipeline.py` | Integrate extract_memories, dedup, quality gate | T1, T2, T3 |
| `plastic_promise/core/quality_gate.py` | **New** — QualityGate scoring module | T3 |
| `plastic_promise/memory/soul_memory.py` | MemoryGC.merge_similar() | T4 |
| `plastic_promise/core/lancedb_store.py` | search_similar() convenience method | T2, T4 |
| `plastic_promise/core/constants.py` | New quality gate threshold constants | T3 |

## 3. Task 1: smart_extractor Pipeline Integration

### 3.1 Integration Point

`extract_memories()` is called inside `store_urgent()`, before the `raw` stage. This makes extraction the first gate every memory passes through.

### 3.2 Behavior

- `extract_memories()` returns **0 items**: pure noise → return `None`, skip pipeline entirely
- Returns **1 item**: return `str` (memory_id), backward compatible
- Returns **N > 1 items**: all N records enter buffer; returns `str` (first memory_id only)
  - Caller (`memory_store` MCP tool) expects a single `str` for SSE notifications, entity edges, and JSON response
  - Remaining extracted memories are still processed through pipeline — they just aren't named in the return value
- Category injected as `cat:{category}` tag to enhance domain matching

### 3.3 Buffer Record Extension

Each buffer record gains an `"extracted"` dict:

```python
"extracted": {
    "category": "preference",        # from ExtractedMemory.category
    "l0_abstract": "...",            # ≤80 chars one-liner
    "l1_summary": "...",             # ≤300 chars structured summary
    "confidence": 0.85,              # extraction confidence
    "importance": 0.75,              # scaled from confidence
}
```

### 3.4 Key Code Changes (pipeline.py)

`store_urgent()` — add extraction call before buffer creation.
`_process_raw_to_tagged()` — use extracted.category to enhance tag quality.
`_process_embedded_to_migrate()` — pass extracted fields to RecMem.store() metadata.

## 4. Task 2: Vector Dedup (Collision Detection)

### 4.1 Mechanism

In `_process_embedded_to_migrate()`, after embedding is generated, query LanceDB for the single most similar existing memory. If cosine similarity ≥ 0.85, treat as duplicate.

### 4.2 Duplicate Handling

Instead of writing a new record:
1. Increment existing memory's `access_count` by 1
2. Increment existing memory's `worth_success` by 1
3. Update `last_accessed` to now
4. Persist counters to SQLite
5. Remove buffer entry (skip RecMem.store())

### 4.3 LanceDBStore.search_similar()

```python
def search_similar(self, vector: list[float], k: int = 5) -> list[tuple[str, float]]:
    """Return (memory_id, similarity_score) for top-k similar vectors.
    Uses existing ANN search with cosine metric, converts distance to similarity.
    """

def check_duplicate(self, vector: list[float], threshold: float = 0.85) -> Optional[str]:
    """Thin wrapper: search_similar(k=1), return memory_id if score ≥ threshold, else None."""
    results = self.search_similar(vector, k=1)
    if results and results[0][1] >= threshold:
        return results[0][0]
    return None
```

### 4.4 Dedup Path (in _process_embedded_to_migrate)

```python
dup_id = self._lancedb.check_duplicate(vec, threshold=0.85)
if dup_id:
    # Direct dict manipulation — follows existing pattern (pipeline.py:256-282)
    engine._memories[dup_id]["access_count"] += 1
    engine._memories[dup_id]["worth_success"] += 1
    self.rec_mem._records[dup_id].access_count += 1
    self.rec_mem._records[dup_id].worth_success += 1
    # SQLite incremental UPDATE
    engine._sqlite._conn.execute(
        "UPDATE memories SET access_count = access_count + 1, worth_success = worth_success + 1 WHERE id = ?",
        (dup_id,)
    )
    del self._buffer[mid]
    continue
```

### 4.5 Graceful Degradation

LanceDB unavailable → skip dedup, log warning, proceed with normal store. Dedup is a quality improvement, not a hard gate.

## 5. Task 3: Multi-Feature Entry Scoring (QualityGate)

### 5.1 Module: `plastic_promise/core/quality_gate.py`

```python
class QualityGate:
    """Composite gating: 4 dimensions × equal weight → gate_score."""

    WEIGHTS = {
        "confidence": 0.25,
        "relevance": 0.25,
        "freshness": 0.25,
        "info_density": 0.25,
    }

    THRESHOLD_STORE = 0.5    # ≥0.5: store normally
    THRESHOLD_LOW = 0.3      # 0.3-0.5: store with low_quality tag
                              # <0.3: discard (or fuzzy buffer)
```

### 5.2 Four Dimensions

#### Confidence (0.25)
- **Source**: `ExtractedMemory.confidence` from smart_extractor
- **Rule match**: keyword hit ratio; **LLM fallback**: LLM classification confidence (base 0.5)
- **Direct**: `score = extracted.get("confidence", 0.5)`

#### Relevance (0.25)
- **Source**: DomainManager tag-to-domain matching
- **With domain_hint**: `matched_tags / len(tags) * 1.5`, capped at 1.0
- **Without domain_hint**: defaults to 0.5 (neutral)
- **domain_hint**: from `classified` stage domain assignment

#### Freshness (0.25)
- **Source**: Reuse Direction A's time decay logic
- **At write time**: `created_at = now` → freshness ≈ 1.0 (brand new)
- **At re-import** (pack_import): computed from original timestamp via Weibull decay
- **Purpose**: Prevent stale memories from re-entering pool during import

#### Information Density (0.25)
- **Source**: ExtractedMemory L0/L1/L2 completeness + structural info
- **L0 score** (0.3): `l0_abstract` exists and len > 10
- **L1 score** (0.3): `l1_summary` exists and len > 20
- **L2 score** (0.2): `l2_content` exists and len > 50
- **Structure score** (0.2): has category AND tags

### 5.3 Graceful Defaults (extracted field missing)

When `extracted` is absent (direct `memory_store` call, extraction error, or bypass path):

| Dimension | Default | Rationale |
|-----------|---------|-----------|
| confidence | 0.5 | Neutral — no extraction signal |
| relevance | 0.5 | Neutral — no domain hint |
| freshness | 1.0 | Brand new write, created_at = now |
| info_density | **0.5** | Generous default for direct writes (not 0.0, which would unfairly filter user-initiated stores) |

Combined: `(0.5+0.5+1.0+0.5) × 0.25 = 0.625`, comfortably above the 0.5 store threshold.

### 5.4 Decision Matrix

| gate_score | Action |
|------------|--------|
| ≥ 0.5 | Store normally |
| 0.3 — 0.5 | Store with `low_quality` tag in metadata |
| < 0.3 | Discard (or route to `fuzzy_buffer` for manual review) |

### 5.4 Pipeline Integration

Called in `_process_embedded_to_migrate()`, after dedup check but before `RecMem.store()`.

```python
gate_score = QualityGate().score(
    extracted=record.get("extracted", {}),
    tags=record.get("tags", []),
    domain_hint=record.get("domain", "uncategorized"),
)
if gate_score >= QualityGate.THRESHOLD_STORE:
    # store normally
elif gate_score >= QualityGate.THRESHOLD_LOW:
    record["metadata"]["quality"] = "low_quality"
    # store with tag
else:
    del self._buffer[mid]  # discard
    continue
```

## 6. Task 4: Similar Memory Merge (Compression)

### 6.1 Integration: MemoryGC.merge_similar()

Added to `MemoryGC.collect()` cycle, executed after `mark_decaying()` and before `forget()`.

### 6.2 Algorithm

```
For each memory with a vector:
  1. LanceDB.search_similar(vector, k=3) → top-3 similar
  2. Filter: similarity ≥ 0.70, skip self-matches
  3. For each pair: survivor = max(worth_score, created_at as tiebreaker)
  4. merged record: content_abstract → survivor.metadata["merged_from"]
  5. merged record: set metadata["merged_into"] = survivor_id
  6. Remove merged record from engine._memories (retrieval layer)
  7. Keep merged record in SQLite with merged_into marker (7-day audit trail)
```

### 6.3 Survivor Selection

Priority order:
1. **Highest `worth_score`** (feedback-verified value)
2. **Most recent `created_at`** (tiebreaker when scores equal)

New memories default to `worth_score = 0.5` (Wilson lower bound, `WORTH_MIN_OBSERVATIONS = 5`). This prevents new
memories from being incorrectly merged away — they compete fairly, and `created_at` breaks ties correctly.

### 6.4 Survivor Metadata Structure

```python
survivor.metadata["merged_from"] = [
    {
        "memory_id": "abc123",
        "content_abstract": "User prefers Rust for backend due to...",  # first 80 chars
        "merged_at": "2026-06-30T12:00:00",
        "worth_score": 0.45,
    },
]
```

### 6.5 Retrieval Filtering

Merged records are **immediately removed from `engine._memories`** so `memory_recall` never returns them. The SQLite audit trail preserves them for 7 days (next GC cycle cleans them permanently).

### 6.6 Dry Run Output

```python
{
    "candidates_found": 15,       # similar pairs discovered
    "would_merge": 12,            # pairs that would be merged (deduped)
    "would_free": 3,              # redundant records to be removed
    "merged_pairs": [             # sample pairs for preview
        {"survivor": "mem_001", "merged": ["mem_002", "mem_003"], "similarity": 0.82},
        {"survivor": "mem_005", "merged": ["mem_007"], "similarity": 0.74},
    ],
}
```

### 6.7 Performance

- Full-pool scan: O(n) × ANN search O(log n) = O(n log n)
- 2000 memories × ~5ms ANN = ~10s total per GC cycle (7-day interval)
- Acceptable overhead for a batched background operation

## 7. Constants

```python
# Quality Gate (new in constants.py)
QUALITY_GATE_WEIGHTS = {"confidence": 0.25, "relevance": 0.25, "freshness": 0.25, "info_density": 0.25}
QUALITY_GATE_THRESHOLD_STORE = 0.5
QUALITY_GATE_THRESHOLD_LOW = 0.3

# Dedup & Merge (new in constants.py)
DEDUP_SIMILARITY_THRESHOLD = 0.85      # cosine similarity ≥ this → duplicate
MERGE_SIMILARITY_THRESHOLD = 0.70      # cosine similarity ≥ this → merge candidate
MERGE_TOP_K = 3                        # top-k similar to check per memory
MERGE_AUDIT_RETENTION_DAYS = 7         # merged records kept in SQLite before GC
```

## 8. Error Handling

| Failure | Behavior |
|---------|----------|
| `extract_memories()` raises | Catch, fall back to original raw content (no extraction metadata) |
| LanceDB unavailable for dedup | Skip dedup, log warning, proceed with store |
| LanceDB unavailable for merge | `merge_similar()` returns `{"error": "lancedb_unavailable"}`, GC continues |
| QualityGate.score() raises | Default to score=0.5 (neutral), store normally |
| SQLite write fails during dedup counter update | Log error, still remove buffer entry (memory stored, counters not incremented) |

## 9. Verification

### 9.1 Unit Tests
- `test_quality_gate.py` — 4 dimension scoring, threshold decisions, edge cases (empty tags, missing extracted)
- `test_pipeline_dedup.py` — mock LanceDB, verify duplicate detection and counter increment
- `test_memory_merge.py` — mock LanceDB results, verify survivor selection and metadata structure

### 9.2 Integration Tests
- Full pipeline run with `extract_memories` integrated, verify extracted fields flow to RecMem
- GC cycle with `merge_similar` enabled, verify merged records removed from retrieval
- Dry run report validity

### 9.3 Manual Verification
- `memory_stats` — verify dedup increments worth_success on existing records
- `memory_list` — verify low_quality tag appears for borderline memories
- `memory_recall` — verify merged records are not returned

## 10. Resolved Design Decisions

| # | Decision | Resolution | Rationale |
|---|----------|------------|-----------|
| 1 | `store_urgent()` return type for multi-extraction | Always return `str` (first memory_id) | `memory_store` MCP tool uses `fuzzy_id` as single string for edges, SSE notifications, and JSON response (tools/memory.py:147-174). All extracted records still enter pipeline buffer. |
| 2 | QualityGate defaults when `extracted` is missing | `info_density` defaults to 0.5 (not 0.0) | Combined score = 0.625, passes store threshold. Prevents direct `memory_store` writes from being unfairly filtered. |
| 3 | `worth_score` timing for merge (new memories) | No risk — default is 0.5, not 0 | Wilson lower bound returns 0.5 when `n < WORTH_MIN_OBSERVATIONS` (5). `created_at` tiebreaker correctly handles equal scores. New memories won't be incorrectly merged away. |
