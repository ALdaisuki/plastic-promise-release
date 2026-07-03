# 03 — Smart Extraction and Memory Lifecycle Roadmap

> Current status: active roadmap. Some tier behavior may already exist, but category-aware merge, chunking, compaction, and throttling remain open unless source verification proves otherwise.

## Status Summary

| Area | Status | Evidence | Remaining work |
|---|---|---|---|
| Real-time tier promotion/demotion | Partial | Context engine tier logic appears to exist. | Verify thresholds, demotion behavior, persistence, and tests. |
| Category-aware merge rules | Planned | No verified category-specific rule engine in this pass. | Add merge/update/append/contradict semantics per category. |
| Content chunking | Planned | No verified LanceDB chunk schema migration in this pass. | Add chunker, schema migration, and parent-memory result mapping. |
| Memory compaction | Planned | Existing similar-memory merge is not the same as progressive compaction. | Add cluster cooldown, archive behavior, and optional LLM merge. |
| Extraction throttling | Planned | No verified sliding-window throttle in this pass. | Rate-limit LLM fallback extraction. |

## 1. Real-time Tier Promotion/Demotion

### Goal

Frequently used memories should move toward warmer tiers during active use, not only during periodic daemon scans. Decayed or low-value memories should be demoted conservatively.

### Tasks

- Verify whether `_maybe_adjust_tier()` or equivalent exists in the current `ContextEngine` path.
- Add explicit thresholds to constants or configuration.
- Persist tier changes consistently to SQLite and LanceDB metadata where applicable.
- Keep daemon tier migration as a batch backstop.
- Add tests for L1-to-L2, L2-to-L3, and demotion paths.

## 2. Category-Aware Merge Rules

### Goal

Deduplication should respect memory category. A preference, fact, decision, event, entity, and pattern should not all follow the same “newer replaces older” rule.

### Target behavior

| Category | Preferred action |
|---|---|
| preference | Merge/update existing preference. |
| fact | Replace only when newer or better-supported. |
| decision | Usually append; decisions are historical state. |
| entity | Merge/update entity profile. |
| event | Append; events are unique occurrences. |
| pattern | Merge if semantically equivalent, otherwise append. |

### Extended decisions

Future semantic merge can support:

```text
CREATE, MERGE, SKIP, SUPERSEDE, SUPPORT, CONTEXTUALIZE, CONTRADICT
```

## 3. Content Chunking for Long Memories

### Goal

Long memories should be embedded as chunks while still returning the parent memory.

### Proposed design

```text
memory_id
  -> chunk_index
  -> total_chunks
  -> chunk_text
  -> full_text / parent reference
  -> vector
```

### Tasks

- Add a sentence/paragraph-aware chunker.
- Add LanceDB schema migration.
- Update insertion to write multiple chunks.
- Update retrieval to return parent memory with best matching chunk context.
- Add tests for long bilingual content and boundary overlap.

## 4. Memory Compaction

### Goal

Merge clusters of old, similar memories into higher-quality summaries without losing provenance.

### Planned behavior

- Only compact memories older than a minimum age.
- Use a stricter similarity threshold than routine duplicate detection.
- Track cooldown so the same cluster is not repeatedly compacted.
- Archive old entries rather than destructive deletion.
- Keep the feature disabled by default until validated.

## 5. Extraction Throttling

### Goal

Prevent rapid-fire conversations from triggering excessive LLM fallback extraction.

### Planned behavior

- Sliding-window counter per process or persisted runtime state.
- Configurable `PP_EXTRACTION_MAX_PER_HOUR`.
- Prefer local/rule extraction when near the limit.
- Label skipped low-value extractions explicitly.

## Acceptance Criteria

- Category merge tests cover all six extraction categories.
- Chunked retrieval returns the correct parent memory and chunk excerpt.
- Compaction produces traceable archive links.
- Throttling protects LLM fallback without blocking high-confidence local extraction.
