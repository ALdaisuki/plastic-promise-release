# 05 — Integration Roadmap

> How TODO items integrate with existing Plastic Promise systems.

## Integration Points

Each TODO item touches specific existing modules. This document maps the connections.

### P0 Items (Week 1)

```
Query Expansion                    Reranker Upgrade                  Decay-in-Ranking
─────────────────                  ────────────────                  ────────────────
NEW: query_expander.py             MODIFY: reranker.py               MODIFY: context_engine.py
  │                                  │                                 │
  ├─ ContextEngine._supply_python()  ├─ ContextEngine._apply_rerank()  ├─ ContextEngine._supply_python()
  │  (called before text/vector      │  (replace Ollama-only with      │  (new _apply_decay_boost() in
  │   retrieval loop)                │   multi-provider chain)          │   single-pass item loop)
  │                                  │                                 │
  └─ Env: PP_QUERY_EXPANSION=1       ├─ Env: PP_RERANK_PROVIDERS       └─ Env: PP_DECAY_IN_RANKING=1
                                     │  (jina,siliconflow,ollama,cosine)   (default on)
                                     └─ Always-on (remove PP_RECALL_RERANK gate)
```

**Integration checkpoints:**
- All three items modify `_supply_python()` — apply in order: expansion → retrieval → decay
- Reranker upgrade is the riskiest (network dependency) — keep Ollama+cosine fallback intact
- Decay-in-ranking is the safest (pure computation, no I/O)

### P1 Items (Week 2)

```
Tier Promotion                     Merge Rules                      Chunking
────────────                       ───────────                      ────────
MODIFY: context_engine.py          MODIFY: smart_extractor.py        NEW: chunker.py
  │                                  │                                 │
  ├─ _maybe_adjust_tier()            ├─ CATEGORY_MERGE_RULES           ├─ chunk_content()
  │  (called from _text_retrieval    ├─ decide_merge_action()          │  (called from LanceDBStore.insert)
  │   and increment_field)           │  (called from pipeline.py       │
  │                                  │   dedup flow)                   ├─ MODIFY: lancedb_store.py
  ├─ MODIFY: constants.py            │                                 │  (schema v2 with chunk_index,
  │  (TIER_THRESHOLDS)               └─ MODIFY: pipeline.py            │   total_chunks, full_text)
  │                                  │  (use category-aware rules      │
  └─ KEEP: scan_tier_migration       │   instead of threshold-only)    ├─ SCHEMA MIGRATION: v1 → v2
     (batch backstop)                │                                 │  (existing vectors need
                                     └─ No env var needed              │   re-indexing)
                                     (always-on behavior change)       │
                                                                       └─ Env: PP_CHUNK_SIZE=500
                                                                          PP_CHUNK_OVERLAP=100

Vector MMR
──────────
MODIFY: lancedb_store.py (add get_vector)
MODIFY: context_engine.py (_apply_mmr fix)
  │
  └─ Reuses chunker's get_vector() if both implemented
```

**Integration checkpoints:**
- Chunking is the riskiest P1 (schema migration) — do it last in Week 2
- Tier promotion shares access_count tracking with decay-in-ranking (P0) — compatible
- Vector MMR reuses LanceDBStore.get_vector() added for chunking — implement chunking first

### P2 Items (Week 3)

```
Session Recovery                   Benchmarking                     Emoji Detection
────────────────                   ─────────────                     ───────────────
NEW: session_recovery.py            NEW: benchmark.py                 MODIFY: noise_filter.py
  │                                  │                                 │
  ├─ Called from MCP server          ├─ ContextEngine.supply()         ├─ is_noise() — add emoji check
  │  startup (after heavy_init)      │  (opt-in timing via             │
  │                                  │   PP_BENCHMARK=1)               └─ ~10 lines, no env var
  ├─ Ghost-vector cleanup            │
  │  (LanceDB has row, SQLite        ├─ SQLite: benchmark_history
  │   doesn't → delete from LDB)     │  (last 100 measurements)
  │                                  │
  ├─ Orphan re-index                 └─ MCP: system(action="benchmark")
  │  (SQLite has row, LDB doesn't
  │   → embed + insert)
  │
  └─ Stale claim release
     (Hunter Guild tasks >5min
      in "claimed" state)

Iron Rules
──────────
MODIFY: step-closure handler
  │
  ├─ Extract decision principle from
  │  lesson + root_cause + optimization
  │
  ├─ memory_store(type="principle")
  │  (with backlink to source)
  │
  └─ MODIFY: CLAUDE.md step-closure section
     (document dual-layer expectation)
```

### P3 (Backlog)

```
Multi-Provider API
──────────────────
RESEARCH: provider landscape
  │
  ├─ Which free tiers exist?
  ├─ Dimension mismatch handling?
  │  (Ollama 1024 vs OpenAI 1536 vs Voyage 1024)
  │
  └─ Decision: implement or defer?
```

## File Change Summary

| File | P0 | P1 | P2 | P3 | Total Changes |
|------|----|----|----|----|---------------|
| `context_engine.py` | 2 (decay, expansion hook) | 2 (tier, MMR) | 1 (benchmark) | — | 5 |
| `reranker.py` | 1 (multi-provider) | — | — | 1 (API abst) | 2 |
| `lancedb_store.py` | — | 2 (get_vector, schema v2) | — | — | 2 |
| `smart_extractor.py` | — | 2 (merge rules, throttle) | — | — | 2 |
| `pipeline.py` | — | 1 (merge rules) | — | — | 1 |
| `soul_memory.py` | — | 1 (compaction) | — | — | 1 |
| `noise_filter.py` | — | — | 1 (emoji) | — | 1 |
| `constants.py` | — | 1 (tier thresholds) | — | 1 (config-driven) | 2 |
| `embedder.py` | — | — | — | 1 (multi-prov) | 1 |
| **NEW: query_expander.py** | 1 | — | — | — | 1 |
| **NEW: chunker.py** | — | 1 | — | — | 1 |
| **NEW: benchmark.py** | — | — | 1 | — | 1 |
| **NEW: export_obsidian.py** | — | — | 1 | — | 1 |
| **TOTAL** | 4 | 10 | 4 | 3 | **21 file changes** |

## Rollback Strategy

Each item is independently gated by an env var:

| Feature | Env Var | Default | Rollback |
|---------|---------|---------|----------|
| Query Expansion | `PP_QUERY_EXPANSION` | 1 | Set to 0 |
| Multi-Provider Rerank | `PP_RERANK_PROVIDERS` | `jina,siliconflow,ollama,cosine` | Set to `ollama` |
| Decay-in-Ranking | `PP_DECAY_IN_RANKING` | 1 | Set to 0 |
| Tier Auto-Promotion | `PP_TIER_AUTO_PROMOTE` | 1 | Set to 0 |
| Chunking | `PP_CHUNK_SIZE` | 500 | Set to 0 (disable) |
| Vector MMR | `PP_MMR_VECTOR` | 1 | Set to 0 |
| Benchmarking | `PP_BENCHMARK` | 0 | Already off by default |

All new features default to ON for P0/P1, OFF for P2 — reflecting their maturity level.

## Testing Strategy

```
P0 items:
  - Unit: query_expander output shape, reranker provider fallback, decay boost range
  - Integration: supply() with all three enabled → compare layer distribution vs baseline
  - Manual: 10 test queries, rate result quality 1-5

P1 items:
  - Unit: tier thresholds, merge action for each category, chunk boundaries
  - Integration: LanceDB schema migration v1→v2, chunk-aware search
  - Manual: long memory (>1000 chars) retrieval quality before/after chunking

P2 items:
  - Unit: emoji regex, session recovery steps, benchmark timer
  - Integration: MCP server restart → recovery runs, benchmark_history writes
```
