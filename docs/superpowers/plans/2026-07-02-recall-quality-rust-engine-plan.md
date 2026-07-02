# Recall Quality Fix & Rust Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Phase 1 fixes Python recall pipeline (BM25, MMR, length-norm, rerank, LanceDB rebuild) so `memory_recall` returns correct relevant memories. Phase 2 migrates the same pipeline to Rust as full compute engine while retaining Python as persistent fallback.

**Architecture:** Phase 1 upgrades `_text_retrieval` from word-overlap to Okapi BM25 with IDF weighting, inserts MMR diversity + length normalization + optional rerank into `_supply_python`, and rebuilds LanceDB from SQLite. Phase 2 aligns Rust schema, adds `Bm25Index` with version-checked lazy refresh, ports the 13-stage pipeline, and activates domain models (WeibullDecay/WilsonWorth/Tier).

**Tech Stack:** Python 3.13, LanceDB (Python SDK), Ollama mxbai-embed-large, Rust + pyo3 + rusqlite

**Spec:** `docs/superpowers/specs/2026-07-02-recall-quality-rust-engine-design.md`

## Global Constraints

- `PP_CORE_MIN_RELEVANCE` env var, default `0.70`
- `PP_RELATED_MIN_RELEVANCE` env var, default `0.40`
- `PP_DIVERGENT_MIN_RELEVANCE` env var, default `0.20`
- `PP_RECALL_RERANK` env var, default `0`
- `PP_RERANK_MODEL` env var, default `""`
- `PP_RERANK_TIMEOUT` env var, default `5.0`
- `PP_FORCE_PYTHON_SUPPLY` env var, default `0` (Phase 2 only)
- BM25: k1=1.2, b=0.75, English stopwords + porter stem, CJK bigram
- MMR: threshold=0.85, penalty=0.70, soft-demote (not remove)
- Length norm: anchor=500 chars, floor=scores × 0.3
- Rerank: 60% cross-encoder + 40% original, 5s timeout
- `MEMORY_VERSION` stored in SQLite table `memory_version(version INTEGER)`
- `_supply_python` preserved as persistent fallback (not deleted)

---

## Phase 1: Python Recall Quality Fix

### Task 1.1: LanceDB Rebuild — `rebuild_all()`

**Files:**
- Modify: `plastic_promise/core/lancedb_store.py` (add `rebuild_all` method after `backfill`)
- Modify: `plastic_promise/core/lancedb_store.py` (add `clear_all` method)

**Interfaces:**
- Consumes: `self._table`, `self._embedder`, `engine._memories` (dict of memory_id → dict with content/tier/category/scope)
- Produces: `LanceDBStore.rebuild_all(engine) -> int` (returns count of re-indexed memories), `LanceDBStore.clear_all() -> None`

- [ ] **Step 1: Add `clear_all()` method to LanceDBStore**

```python
# plastic_promise/core/lancedb_store.py — add after count_rows() method (line ~267)

def clear_all(self) -> int:
    """Delete all rows from the table and return the count that was removed.
    
    After clearing, the table is empty but still exists with its schema and FTS index intact.
    """
    if self._table is None:
        return 0
    try:
        count = self._table.count_rows()
        if count > 0:
            self._table.delete("memory_id IS NOT NULL")
        logger.info("LanceDB: cleared %d rows", count)
        return count
    except Exception as e:
        logger.error("LanceDB clear_all failed: %s", e)
        return 0
```

- [ ] **Step 2: Add `rebuild_all()` method to LanceDBStore**

```python
# plastic_promise/core/lancedb_store.py — add after clear_all()

def rebuild_all(self, engine: object) -> int:
    """Clear LanceDB table and rebuild all vectors from SQLite memories.

    Used when LanceDB vectors are out of sync with SQLite (ghost vectors,
    stale entries, test pollution). Regenerates every vector via embedder.

    Safety: calls clear_all() first, then embeds and inserts each memory.
    Respects LDB_BACKFILL_MAX_PER_CALL env var for batching.

    Args:
        engine: ContextEngine instance with _memories dict.

    Returns:
        Number of memories re-indexed.
    """
    import os as _os
    _max_batch = int(_os.environ.get("LDB_REBUILD_MAX_PER_CALL", "200"))

    removed = self.clear_all()
    logger.info("LanceDB rebuild: removed %d rows, starting re-index", removed)

    memories = getattr(engine, '_memories', {})
    if not memories:
        logger.warning("LanceDB rebuild: engine._memories is empty — nothing to rebuild")
        return 0

    rebuilt = 0
    circuit_open = False

    for mid, mem_data in memories.items():
        if rebuilt >= _max_batch:
            logger.info("LanceDB rebuild: hit batch limit (%d), %d remaining",
                        _max_batch, len(memories) - rebuilt)
            break

        content = mem_data.get("content", "")
        if not content or not content.strip():
            continue

        try:
            if circuit_open:
                continue
            vector = self._embedder.embed(content)
        except Exception as e:
            logger.error("LanceDB rebuild: embedder failed on '%s', circuit open: %s", mid, e)
            circuit_open = True
            continue

        tier = mem_data.get("tier", "L1")
        category = mem_data.get("category", "other")
        scope = mem_data.get("scope", "global")

        try:
            self.insert(mid, vector, content, tier=tier, category=category, scope=scope)
            rebuilt += 1
        except Exception as e:
            logger.error("LanceDB rebuild: insert failed for %s: %s", mid, e)

    logger.info("LanceDB rebuild: complete — %d memories re-indexed", rebuilt)
    return rebuilt
```

- [ ] **Step 3: Trigger rebuild in `_ensure_heavy_init`**

```python
# plastic_promise/core/context_engine.py — in _ensure_heavy_init(), 
# after the LanceDB backfill line (~line 357), add:

# Rebuild check: if LDB has more rows than SQLite (ghost vectors), trigger rebuild
ldb_count = self._ldb.count_rows() if self._ldb else 0
sqlite_count = len(self._memories)
if ldb_count > sqlite_count:
    logger.warning(
        "LanceDB has %d rows but SQLite has %d memories — triggering rebuild to remove %d ghost vectors",
        ldb_count, sqlite_count, ldb_count - sqlite_count
    )
    self._ldb.rebuild_all(self)
```

- [ ] **Step 4: Verify with a quick script**

```bash
cd "F:\Agent\Memory system" && python -c "
from plastic_promise.core.context_engine import ContextEngine
e = ContextEngine()
e._ensure_heavy_init()
ldb = e._ldb.count_rows()
mem = e.memory_count
print(f'LanceDB: {ldb}, SQLite: {mem}, match={ldb <= mem}')
assert ldb <= mem, f'Ghost vectors: LDB {ldb} > SQLite {mem}'
print('PASS: No ghost vectors')
"
```

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/lancedb_store.py plastic_promise/core/context_engine.py
git commit -m "feat(ldb): add rebuild_all() to clear ghost vectors on init"
```

---

### Task 1.2: BM25 Text Retrieval — Replace `_text_retrieval`

**Files:**
- Modify: `plastic_promise/core/context_engine.py:_text_retrieval` (lines 1762-1825)

**Interfaces:**
- Consumes: `self._memories` (dict of memory_id → dict with content/tier/domain/source/owner)
- Produces: `_text_retrieval(task: str, trust_boost: float = 1.0) -> List[tuple[str, float, str, str]]` — same signature as current

- [ ] **Step 1: Add BM25 helpers as module-level functions**

```python
# plastic_promise/core/context_engine.py — add before _text_retrieval (~line 1760)

import re as _re
import math as _math
from collections import Counter as _Counter

# English stopwords — minimal set for BM25
_EN_STOPWORDS = frozenset({
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'shall', 'can', 'need', 'dare', 'ought',
    'used', 'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from',
    'as', 'into', 'through', 'during', 'before', 'after', 'above', 'below',
    'between', 'under', 'again', 'further', 'then', 'once', 'here', 'there',
    'when', 'where', 'why', 'how', 'all', 'both', 'each', 'few', 'more',
    'most', 'other', 'some', 'such', 'only', 'own', 'same', 'so', 'than',
    'too', 'very', 'just', 'because', 'but', 'and', 'or', 'if', 'while',
    'about', 'not', 'this', 'that', 'these', 'those', 'it', 'its',
})

# Porter stemmer — minimal implementation for English
def _porter_stem(word: str) -> str:
    """Minimal Porter stemmer for common English suffixes."""
    w = word.lower()
    if len(w) <= 3:
        return w
    # Step 1a
    if w.endswith('sses'):
        w = w[:-2]
    elif w.endswith('ies'):
        w = w[:-2]
    elif w.endswith('ss'):
        pass  # keep ss
    elif w.endswith('s'):
        w = w[:-1]
    # Step 1b
    if w.endswith('eed'):
        if len(w) > 4:
            w = w[:-1]
    elif w.endswith('ed') and not w.endswith('eed'):
        if len(w) > 3:
            w = w[:-2]
    elif w.endswith('ing'):
        if len(w) > 4:
            w = w[:-3]
    # Step 4
    for suffix in ('ement', 'ment', 'ence', 'ance', 'able', 'ible',
                   'ment', 'ent', 'ant', 'ism', 'ate', 'iti', 'ous',
                   'ive', 'ize', 'ion', 'al', 'er', 'ic', 'ou', 'ly'):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[:-len(suffix)]
            break
    return w


def _tokenize(text: str) -> list[str]:
    """Tokenize text for BM25 indexing.
    
    CJK text: bigram tokens (min 2-char). English: split + filter + stem.
    """
    if not text or not text.strip():
        return []
    has_cjk = bool(_re.search(r'[一-鿿]', text))
    if has_cjk:
        chars = [c for c in text if not c.isspace()]
        return [chars[i] + chars[i+1] for i in range(len(chars) - 1)]
    else:
        words = text.lower().split()
        return [
            _porter_stem(w.strip('.,!?;:()[]{}"\'-'))
            for w in words
            if len(w) >= 3 and w.lower() not in _EN_STOPWORDS
        ]


def _compute_idf(doc_freq: dict[str, int], total_docs: int) -> dict[str, float]:
    """Compute IDF for each term. idf = log((N - df + 0.5) / (df + 0.5) + 1)."""
    return {
        term: _math.log((total_docs - df + 0.5) / (df + 0.5) + 1.0)
        for term, df in doc_freq.items()
    }


def _bm25_score(
    query_terms: list[str],
    doc_terms: list[str],
    idf: dict[str, float],
    avg_doc_len: float,
    k1: float = 1.2,
    b: float = 0.75,
) -> float:
    """Compute Okapi BM25 score for a single document."""
    doc_len = len(doc_terms)
    tf_counts = _Counter(doc_terms)
    score = 0.0
    for term in query_terms:
        if term not in idf:
            continue
        tf = tf_counts.get(term, 0)
        if tf == 0:
            continue
        numerator = tf * (k1 + 1.0)
        denominator = tf + k1 * (1.0 - b + b * doc_len / avg_doc_len)
        score += idf[term] * numerator / denominator
    return score
```

- [ ] **Step 2: Replace `_text_retrieval` method**

```python
# plastic_promise/core/context_engine.py — replace _text_retrieval (lines 1762-1825)

def _text_retrieval(self, task: str, trust_boost: float = 1.0) -> list[tuple]:
    """BM25 text retrieval with IDF weighting.
    
    Builds document frequency table from self._memories on each call
    (192 docs < 5ms). Replaces the old word-overlap matching.
    """
    results = []
    query_terms = _tokenize(task)
    if not query_terms:
        return results

    current_owner = os.environ.get("AGENT_OWNER", "")
    domain_hint = getattr(self, '_domain_hint', None)
    dm = getattr(self, '_dm', None)
    has_dm = dm is not None and domain_hint and domain_hint != "all"
    hint_dom = dm.domains.get(domain_hint) if has_dm else None

    # --- Build DF table and pre-tokenize docs ---
    doc_terms: dict[str, list[str]] = {}
    doc_freq: dict[str, int] = {}
    eligible: list[str] = []

    for mid, mem in self._memories.items():
        mem_owner = mem.get("owner", "")
        if current_owner and mem_owner not in (current_owner, "shared", ""):
            continue
        content = mem.get("content", "")
        if not content.strip():
            continue
        tokens = _tokenize(content)
        if not tokens:
            continue
        doc_terms[mid] = tokens
        eligible.append(mid)
        unique_terms = set(tokens)
        for term in unique_terms:
            doc_freq[term] = doc_freq.get(term, 0) + 1

    if not eligible:
        return results

    # Compute IDF
    total_docs = len(eligible)
    avg_doc_len = sum(len(t) for t in doc_terms.values()) / total_docs if total_docs > 0 else 1.0
    idf = _compute_idf(doc_freq, total_docs)

    # Score each document
    for mid in eligible:
        mem = self._memories[mid]
        tokens = doc_terms[mid]
        raw_score = _bm25_score(query_terms, tokens, idf, avg_doc_len)

        if raw_score <= 0:
            continue

        # Normalize BM25 score to [0, 1] using sigmoid
        score = 1.0 / (1.0 + _math.exp(-raw_score / 3.0))

        # Tier boost — same as old code
        tier = mem.get("tier", "L2")
        if tier == "L1":
            score = min(score * 1.5 * trust_boost, 1.0)
        elif tier == "L3":
            score = score * 0.8 * trust_boost

        # Domain boost
        if has_dm:
            mem_domain = mem.get("domain", "uncategorized")
            if mem_domain == domain_hint:
                score = min(score * 1.3, 1.0)
            elif hint_dom:
                mem_tags = set(mem.get("tags", []))
                if mem_tags & hint_dom.tags:
                    score = min(score * 1.1, 1.0)

        results.append((mid, min(score, 1.0), mem["content"][:300], mem["source"]))

    # Sort by score descending
    results.sort(key=lambda x: x[1], reverse=True)
    return results
```

- [ ] **Step 3: Verify BM25 hit rate**

```bash
cd "F:\Agent\Memory system" && python -c "
from plastic_promise.core.context_engine import ContextEngine
e = ContextEngine()
results = e._text_retrieval('code review scanner data quality fix')
print(f'BM25 hits: {len(results)} (was 11/192 before)')
assert len(results) >= 20, f'Expected >=20 hits, got {len(results)}'
# Check that real memories appear, not just synthetic
contents = [r[2][:80] for r in results[:5]]
print('Top 5:')
for c in contents:
    print(f'  {c}')
# Verify no 'Performance test memory' garbage
test_pollution = [r for r in results if 'Performance test memory' in r[2]]
print(f'Test pollution: {len(test_pollution)} (expected 0)')
assert len(test_pollution) == 0, 'Test data still polluting BM25 results'
print('PASS')
"
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat(bm25): replace word-overlap with Okapi BM25 IDF-weighted text retrieval"
```

---

### Task 1.3: Configurable Layer Thresholds

**Files:**
- Modify: `plastic_promise/core/constants.py` (add env-var reading for thresholds)

**Interfaces:**
- Consumes: `PP_CORE_MIN_RELEVANCE`, `PP_RELATED_MIN_RELEVANCE`, `PP_DIVERGENT_MIN_RELEVANCE` env vars
- Produces: `CONTEXT_LAYERS` dict with dynamically-configured thresholds

- [ ] **Step 1: Update CONTEXT_LAYERS in constants.py**

```python
# plastic_promise/core/constants.py — replace the static CONTEXT_LAYERS definition

import os as _os

CONTEXT_LAYERS = {
    "core": {
        "min_relevance": float(_os.environ.get("PP_CORE_MIN_RELEVANCE", "0.70")),
        "label": "🔵 核心",
    },
    "related": {
        "min_relevance": float(_os.environ.get("PP_RELATED_MIN_RELEVANCE", "0.40")),
        "label": "🟡 关联",
    },
    "divergent": {
        "min_relevance": float(_os.environ.get("PP_DIVERGENT_MIN_RELEVANCE", "0.20")),
        "label": "🟢 发散",
    },
}
```

- [ ] **Step 2: Verify threshold overrides work**

```bash
cd "F:\Agent\Memory system" && python -c "
# Test defaults
from plastic_promise.core.constants import CONTEXT_LAYERS
print(f'Defaults: core={CONTEXT_LAYERS[\"core\"][\"min_relevance\"]}, related={CONTEXT_LAYERS[\"related\"][\"min_relevance\"]}')
assert CONTEXT_LAYERS['core']['min_relevance'] == 0.70
assert CONTEXT_LAYERS['related']['min_relevance'] == 0.40
print('Defaults: PASS')

# Test override
import os, importlib
os.environ['PP_CORE_MIN_RELEVANCE'] = '0.65'
import plastic_promise.core.constants as c
importlib.reload(c)
assert c.CONTEXT_LAYERS['core']['min_relevance'] == 0.65
print('Override: PASS')
"
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/constants.py
git commit -m "feat(thresholds): make CONTEXT_LAYERS thresholds configurable via env vars"
```

---

### Task 1.4: Length Normalization

**Files:**
- Modify: `plastic_promise/core/context_engine.py:_supply_python` (insert after feedback multiplier, before ContextItem construction)

**Interfaces:**
- Consumes: each item's `content` (str) and `score` (float) in the fused loop
- Produces: `_apply_length_norm(score: float, content: str, anchor: int = 500) -> float`

- [ ] **Step 1: Add `_apply_length_norm` helper**

```python
# plastic_promise/core/context_engine.py — add as module-level function before _supply_python

def _apply_length_norm(score: float, content: str, anchor: int = 500) -> float:
    """Normalize score by document length to prevent long documents from dominating.
    
    Formula: score *= 1 / (1 + 0.5 * log2(len / anchor))
    Floor: score * 0.3 (never reduce below 30% of original)
    Short documents (< anchor chars) are not boosted.
    """
    char_len = len(content)
    if char_len <= anchor:
        return score  # no penalty for short docs
    ratio = char_len / anchor
    log_ratio = __import__('math').log2(ratio)
    factor = 1.0 / (1.0 + 0.5 * log_ratio)
    return max(score * factor, score * 0.3)
```

- [ ] **Step 2: Insert length normalization in `_supply_python`**

In `_supply_python`, after the feedback multiplier block (~line 1190) and before ContextItem construction (~line 1192), insert:

```python
            # --- Length normalization (Phase 1.5) ---
            score = _apply_length_norm(score, content)
```

- [ ] **Step 3: Verify length normalization effect**

```bash
cd "F:\Agent\Memory system" && python -c "
from plastic_promise.core.context_engine import _apply_length_norm
import math
# Short doc (under anchor) — no penalty
assert _apply_length_norm(0.8, 'x' * 100) == 0.8
# Medium doc (1000 chars) — moderate penalty
s = _apply_length_norm(0.8, 'x' * 1000)
assert 0.4 < s < 0.7, f'expected moderate penalty, got {s}'
# Long doc (4000 chars) — heavy penalty but above floor
s = _apply_length_norm(0.8, 'x' * 4000)
assert s >= 0.8 * 0.3, f'expected above floor, got {s}'
print(f'Short: 0.80, Medium(1000): {_apply_length_norm(0.8, \"x\"*1000):.3f}, Long(4000): {_apply_length_norm(0.8, \"x\"*4000):.3f}')
print('PASS')
"
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat(length-norm): add length normalization to prevent long-document score monopoly"
```

---

### Task 1.5: MMR Diversity

**Files:**
- Modify: `plastic_promise/core/context_engine.py:_supply_python` (insert after length norm, before layering)
- May need: `plastic_promise/core/context_engine.py` — add `_apply_mmr` method

**Interfaces:**
- Consumes: list of ContextItem (with `.id`, `.relevance`, and `.content`), plus `self._ldb` for vector lookup
- Produces: `_apply_mmr(items: List[ContextItem], threshold: float = 0.85, penalty: float = 0.70) -> List[ContextItem]`

- [ ] **Step 1: Add `_apply_mmr` method to ContextEngine**

```python
# plastic_promise/core/context_engine.py — add as method of ContextEngine class

def _apply_mmr(self, items: list, threshold: float = 0.85, penalty: float = 0.70) -> list:
    """Greedy MMR diversity: demote items with cosine similarity > threshold.
    
    Soft-demotion (not removal): similar items get score *= penalty and
    are deferred to the end of the list, preserving them for lower layers.
    
    Items without vectors (principles, graph nodes) skip similarity checks.
    
    Args:
        items: List of ContextItem with .id, .relevance, .content attributes.
        threshold: Cosine similarity above which items are considered duplicates.
        penalty: Multiplier applied to duplicate item scores.
    
    Returns:
        Reordered list with duplicates demoted to the end.
    """
    if len(items) <= 1:
        return items
    
    # Collect vectors for items that have them
    vectors: dict[str, list[float]] = {}
    zero_vector = [0.0] * 1024
    
    for item in items:
        if item.id.startswith("principle:") or item.id.startswith("graph:"):
            continue
        # Try to get vector from LanceDB
        vec = None
        if self._ldb:
            try:
                similar = self._ldb.search_similar(
                    zero_vector, k=1  # dummy search to check existence
                )
            except Exception:
                similar = []
        # Fallback: load from _memories — we use cached embedding if available
        mem = self._memories.get(item.id, {})
        cached_vec = mem.get("_cached_vector")
        if cached_vec and len(cached_vec) == 1024:
            vectors[item.id] = cached_vec
    
    if len(vectors) < 2:
        return items  # not enough vectors for MMR
    
    # Greedy MMR
    selected: list = []
    deferred: list = []
    
    # Pre-sort by relevance descending
    items_sorted = sorted(items, key=lambda x: x.relevance, reverse=True)
    
    for item in items_sorted:
        item_vec = vectors.get(item.id)
        if item_vec is None:
            selected.append(item)
            continue
        
        # Check similarity against all selected items
        too_similar = False
        for sel in selected:
            sel_vec = vectors.get(sel.id)
            if sel_vec is None:
                continue
            # Cosine similarity (fast 1024d)
            dot = sum(a * b for a, b in zip(item_vec, sel_vec))
            na = __import__('math').sqrt(sum(a * a for a in item_vec))
            nb = __import__('math').sqrt(sum(b * b for b in sel_vec))
            if na < 1e-12 or nb < 1e-12:
                continue
            cos_sim = dot / (na * nb)
            if cos_sim > threshold:
                too_similar = True
                break
        
        if too_similar:
            item.relevance *= penalty
            deferred.append(item)
        else:
            selected.append(item)
    
    # Re-sort deferred by relevance and append
    deferred.sort(key=lambda x: x.relevance, reverse=True)
    return selected + deferred
```

- [ ] **Step 2: Insert MMR in `_supply_python`**

In `_supply_python`, after the fused loop that builds ContextItems and before layering (~line 1214), insert:

```python
        # --- MMR diversity (Phase 1.4) ---
        all_items = pack.core + pack.related + pack.divergent
        all_items = self._apply_mmr(all_items, threshold=0.85, penalty=0.70)
        # Re-distribute to layers based on adjusted relevance
        pack.core.clear()
        pack.related.clear()
        pack.divergent.clear()
        for item in all_items:
            if not item.is_principle:
                if item.relevance >= CONTEXT_LAYERS["core"]["min_relevance"]:
                    item.layer = "core"
                    pack.core.append(item)
                elif item.relevance >= CONTEXT_LAYERS["related"]["min_relevance"]:
                    item.layer = "related"
                    pack.related.append(item)
                elif item.relevance >= CONTEXT_LAYERS["divergent"]["min_relevance"]:
                    item.layer = "divergent"
                    pack.divergent.append(item)
```

This replaces the inline layering block (~lines 1214-1224). The principle-skip logic is preserved.

- [ ] **Step 3: Verify MMR behavior**

```bash
cd "F:\Agent\Memory system" && python -c "
from plastic_promise.core.context_engine import ContextEngine, ContextItem
# Create test items with known vectors
import math
e = ContextEngine()
# Quick smoke test: _apply_mmr exists and handles empty
result = e._apply_mmr([])
assert result == []
# Single item passes through
item = ContextItem(id='test', content='hello', relevance=0.8, source='test', 
                   freshness='valid', is_principle=False, worth_score=0.5,
                   novelty_score=0, confidence=0.5, inspiration_score=0,
                   adoption_count=0, rejection_count=0, times_retrieved=0, decay_status='healthy')
result = e._apply_mmr([item])
assert len(result) == 1
assert result[0].id == 'test'
print('MMR smoke test: PASS')
"
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat(mmr): add greedy MMR diversity to demote near-duplicate results"
```

---

### Task 1.6: Optional Rerank Switch

**Files:**
- Modify: `plastic_promise/core/context_engine.py:_supply_python` (insert after MMR, before layering)

**Interfaces:**
- Consumes: `PP_RECALL_RERANK` env var, top-30 candidate items
- Produces: `_apply_rerank(items: List[ContextItem], query: str) -> List[ContextItem]`, returns items with adjusted relevance

- [ ] **Step 1: Add `_apply_rerank` method**

```python
# plastic_promise/core/context_engine.py — add as method of ContextEngine class

def _apply_rerank(self, items: list, query: str) -> list:
    """Optional cross-encoder rerank via Ollama.
    
    Only runs if PP_RECALL_RERANK=1. Sends top-30 candidates to the rerank
    model for semantic scoring. Blends: 60% cross-encoder + 40% original score.
    Timeout: PP_RERANK_TIMEOUT seconds (default 5s). Failure is silent.
    
    Returns items with adjusted relevance scores. Sets self._last_rerank_status.
    """
    import os as _os
    if _os.environ.get("PP_RECALL_RERANK", "0") != "1":
        self._last_rerank_status = "skipped_disabled"
        return items
    
    if len(items) <= 1:
        self._last_rerank_status = "skipped_disabled"
        return items
    
    # Limit to top-30 candidates for reranking
    candidates = sorted(items, key=lambda x: x.relevance, reverse=True)[:30]
    
    try:
        import json as _json
        import urllib.request as _req
        import urllib.error as _err
        
        model = _os.environ.get("PP_RERANK_MODEL", "")
        timeout = float(_os.environ.get("PP_RERANK_TIMEOUT", "5.0"))
        ollama_url = _os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        
        # Build prompt for cross-encoder-style reranking
        docs = [item.content[:500] for item in candidates]
        payload = _json.dumps({
            "model": model or "mxbai-embed-large",  # fallback to embed model for similarity
            "prompt": f"Query: {query}\n\nRate each document's relevance to the query on a scale of 0-100.\n\n" +
                      "\n\n".join(f"[{i}] {doc}" for i, doc in enumerate(docs)) +
                      "\n\nReturn JSON: {\"scores\": [score0, score1, ...]}",
            "stream": False,
            "options": {"temperature": 0},
        }).encode("utf-8")
        
        req = _req.Request(
            f"{ollama_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = _req.urlopen(req, timeout=timeout)
        result = _json.loads(resp.read().decode("utf-8"))
        response_text = result.get("response", "{}")
        
        # Parse JSON from response
        try:
            scores_data = _json.loads(response_text)
            rerank_scores = scores_data.get("scores", [])
        except _json.JSONDecodeError:
            # Try to extract array directly
            import re as _re_
            match = _re_.search(r'\[[\d,\s]+\]', response_text)
            if match:
                rerank_scores = _json.loads(match.group())
            else:
                raise ValueError("Cannot parse rerank response")
        
        # Blend: 60% cross-encoder + 40% original
        for i, item in enumerate(candidates):
            if i < len(rerank_scores):
                ce_score = float(rerank_scores[i]) / 100.0
                orig_score = item.relevance
                item.relevance = min(ce_score * 0.6 + orig_score * 0.4, 1.0)
                # Floor: never reduce below 50% of original
                item.relevance = max(item.relevance, orig_score * 0.5)
        
        self._last_rerank_status = "completed"
        
    except Exception as e:
        logger = __import__('logging').getLogger("plastic-promise")
        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
            self._last_rerank_status = "skipped_timeout"
        elif "connection" in str(e).lower() or "refused" in str(e).lower():
            self._last_rerank_status = "skipped_ollama_down"
        else:
            self._last_rerank_status = "skipped_no_model"
        logger.info("Rerank skipped: %s", self._last_rerank_status)
    
    return items
```

- [ ] **Step 2: Insert rerank in `_supply_python`**

After MMR diversity block and before layering, insert:

```python
        # --- Rerank (Phase 1.6, optional) ---
        all_items = self._apply_rerank(all_items, task_description)
        pack.audit_metadata["rerank_status"] = getattr(self, '_last_rerank_status', 'skipped_disabled')
```

- [ ] **Step 3: Verify rerank disabled by default**

```bash
cd "F:\Agent\Memory system" && python -c "
from plastic_promise.core.context_engine import ContextEngine
e = ContextEngine()
items = []
status = e._apply_rerank(items, 'test')
print(f'Rerank status (empty): {e._last_rerank_status}')
assert e._last_rerank_status == 'skipped_disabled'
print('PASS: rerank disabled by default')
"
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat(rerank): add optional Ollama cross-encoder rerank behind PP_RECALL_RERANK=1"
```

---

### Task 1.7: End-to-End Verification

**Files:**
- Create: `tests/test_recall_quality.py` (verification script, not automated test suite)

**Interfaces:**
- Consumes: Phase 1.1-1.6 outputs
- Produces: Verification report

- [ ] **Step 1: Create verification script**

```python
# tests/test_recall_quality.py
"""End-to-end recall quality verification for Phase 1."""

import sys, os, json

def test_recall_quality():
    from plastic_promise.core.context_engine import ContextEngine
    from plastic_promise.core.embedder import get_embedder
    
    engine = ContextEngine()
    engine._ensure_heavy_init()
    embedder = get_embedder(fallback_on_error=True)
    
    query = "code review scanner data quality fix"
    vec = embedder.embed(query)
    pack = engine.supply(query, vec, "code_generation", "global")
    
    audit = pack.audit_metadata
    
    # === Phase 1 Verification Checks ===
    failures = []
    
    # Check 1: No ghost vectors
    ldb_count = int(audit.get("ldb_rows", "0"))
    mem_count = int(audit.get("memory_pool_size", "0"))
    if ldb_count > mem_count:
        failures.append(f"Ghost vectors: LDB {ldb_count} > SQLite {mem_count}")
    print(f"  [{'PASS' if ldb_count <= mem_count else 'FAIL'}] LDB rows: {ldb_count} <= SQLite: {mem_count}")
    
    # Check 2: Vector search active
    vec_status = audit.get("vector_search", "fallback")
    if vec_status != "active":
        failures.append(f"Vector search not active: {vec_status}")
    print(f"  [{'PASS' if vec_status == 'active' else 'FAIL'}] Vector search: {vec_status}")
    
    # Check 3: Core has >= 3 relevant items
    core_count = len(pack.core)
    if core_count < 3:
        failures.append(f"Core count {core_count} < 3")
    print(f"  [{'PASS' if core_count >= 3 else 'FAIL'}] Core items: {core_count}")
    
    # Check 4: Related has items
    related_count = len(pack.related)
    if related_count < 3:
        failures.append(f"Related count {related_count} < 3")
    print(f"  [{'PASS' if related_count >= 3 else 'FAIL'}] Related items: {related_count}")
    
    # Check 5: No test pollution in top results
    all_content = " ".join(item.content for item in pack.core + pack.related)
    if "Performance test memory" in all_content:
        failures.append("Test pollution detected in results")
    print(f"  [{'PASS' if 'Performance test memory' not in all_content else 'FAIL'}] No test pollution")
    
    # Check 6: Principles activated (dict format)
    principles = pack.activated_principles
    if len(principles) < 2:
        failures.append(f"Only {len(principles)} principles activated")
    if principles and isinstance(principles[0], dict):
        has_content = all("content" in p for p in principles)
    else:
        has_content = False
    print(f"  [{'PASS' if len(principles) >= 2 and has_content else 'FAIL'}] Principles: {len(principles)} (dict format: {has_content})")
    
    # Check 7: BM25 hit rate
    text_results = engine._text_retrieval(query)
    if len(text_results) < 20:
        failures.append(f"BM25 hits {len(text_results)} < 20")
    print(f"  [{'PASS' if len(text_results) >= 20 else 'FAIL'}] BM25 hits: {len(text_results)}")
    
    # Print top results for manual review
    print("\n--- Top Core Results ---")
    for item in pack.core[:5]:
        print(f"  [{item.relevance:.3f}] {item.content[:120]}...")
    print("\n--- Top Related Results ---")
    for item in pack.related[:5]:
        print(f"  [{item.relevance:.3f}] {item.content[:120]}...")
    
    if failures:
        print(f"\n{failures.__len__()} FAILURES:")
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    else:
        print("\nAll checks PASSED")
        return 0

if __name__ == "__main__":
    sys.exit(test_recall_quality())
```

- [ ] **Step 2: Run verification**

```bash
cd "F:\Agent\Memory system" && python tests/test_recall_quality.py
```

Expected: All 7 checks pass. Core ≥ 3 items. No test pollution. BM25 ≥ 20 hits.

- [ ] **Step 3: Verify MCP memory_recall end-to-end**

```bash
# Ensure MCP server is running, then:
cd "F:\Agent\Memory system" && python -c "
import urllib.request, json
# Quick health check
resp = urllib.request.urlopen('http://127.0.0.1:9020/health')
print(json.loads(resp.read()))
"
```

- [ ] **Step 4: Commit verification**

```bash
git add tests/test_recall_quality.py
git commit -m "test(recall): add end-to-end recall quality verification script"
```

---

## Phase 2: Rust Engine Refactor

### Task 2.1: Schema Alignment — MemoryRecord +4 columns

**Files:**
- Modify: `rust/context-engine-core/src/memory_worth.rs` (add fields)
- Modify: `rust/context-engine-core/src/storage/schema.rs` (update DDL, SELECT, row mapping)
- Modify: `rust/context-engine-core/src/storage/sqlite_impl.rs` (update `row_to_record`, `store`, `update`)

**Interfaces:**
- Consumes: existing MemoryRecord struct, SQLite DDL strings
- Produces: updated MemoryRecord with `tags: Vec<String>`, `domain: String`, `decay_multiplier: f64`, `effective_half_life: f64`

- [ ] **Step 1: Add fields to MemoryRecord**

```rust
// rust/context-engine-core/src/memory_worth.rs — add to MemoryRecord struct

pub tags: Vec<String>,           // JSON array of tags, default vec![]
pub domain: String,               // domain label, default "uncategorized"
pub decay_multiplier: f64,        // Weibull decay coefficient, default 1.0
pub effective_half_life: f64,     // effective half-life in days, default 3.0
```

Also update `MemoryRecord::new()`, `from_storage()`, and all constructors to include these fields with defaults.

- [ ] **Step 2: Add `parse_tags` helper**

```rust
// rust/context-engine-core/src/memory_worth.rs

fn parse_tags(raw: &str) -> Vec<String> {
    if raw.is_empty() || raw == "[]" || raw == "null" {
        return vec![];
    }
    serde_json::from_str(raw).unwrap_or_default()
}
```

- [ ] **Step 3: Update SQLite DDL in schema.rs**

Add columns to `SQL_CREATE_MEMORIES`:
```sql
tags TEXT NOT NULL DEFAULT '[]',
domain TEXT NOT NULL DEFAULT 'uncategorized',
decay_multiplier REAL NOT NULL DEFAULT 1.0,
effective_half_life REAL NOT NULL DEFAULT 3.0,
```

Update `SQL_UPSERT_MEMORY` to include 4 new columns. Update `SQL_GET_BY_ID` and the main SELECT to return all columns.

- [ ] **Step 4: Update `row_to_record` in sqlite_impl.rs**

Map 4 new columns (indices shift by +4 after existing columns):
```rust
let tags_raw: String = row.get(14)?;
let domain: String = row.get(15)?;
let decay_multiplier: f64 = row.get(16)?;
let effective_half_life: f64 = row.get(17)?;
```

Call `parse_tags(&tags_raw)` and pass to `MemoryRecord::from_storage(...)`.

- [ ] **Step 5: Build and test**

```bash
cd rust/context-engine-core && cargo build --release 2>&1
cargo test 2>&1
```

- [ ] **Step 6: Commit**

```bash
git add rust/context-engine-core/src/
git commit -m "feat(rust): align MemoryRecord schema with Python — add tags, domain, decay_multiplier, effective_half_life"
```

---

### Task 2.2: Read-Only SQLite Connection

**Files:**
- Modify: `rust/context-engine-core/src/storage/sqlite_impl.rs` (add `open_readonly`)

- [ ] **Step 1: Add `open_readonly` constructor**

```rust
// rust/context-engine-core/src/storage/sqlite_impl.rs

use rusqlite::{Connection, OpenFlags};

impl SqliteStorage {
    /// Open a SQLite database in read-only mode.
    /// 
    /// Used by the Rust compute engine to read from plastic_memory.db
    /// while Python writes concurrently. WAL mode allows concurrent reads.
    /// Does NOT run DDL — assumes tables already exist.
    pub fn open_readonly(path: &str) -> Result<Self, String> {
        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
        )
        .map_err(|e| format!("Failed to open SQLite read-only: {}", e))?;
        Ok(Self { conn })
    }
}
```

- [ ] **Step 2: Update `new_with_backends` to use `open_readonly`**

In `context_engine.rs`, change `new_with_backends` to use `SqliteStorage::open_readonly(_sqlite_path)` instead of `SqliteStorage::open(":memory:")`.

- [ ] **Step 3: Build and verify**

```bash
cd rust/context-engine-core && cargo build --release
cargo test
```

- [ ] **Step 4: Commit**

```bash
git add rust/context-engine-core/src/storage/sqlite_impl.rs rust/context-engine-core/src/context_engine.rs
git commit -m "feat(rust): add read-only SQLite connection for concurrent Python write safety"
```

---

### Task 2.3: BM25 Index with Version-Checked Lazy Refresh

**Files:**
- Create: `rust/context-engine-core/src/retrieval/bm25.rs`
- Modify: `rust/context-engine-core/src/retrieval/mod.rs` (add `mod bm25`)
- Modify: `rust/context-engine-core/src/context_engine.rs` (add Bm25Index to struct, check version in supply)

- [ ] **Step 1: Create `bm25.rs` with Bm25Index**

```rust
// rust/context-engine-core/src/retrieval/bm25.rs

use std::collections::HashMap;

const K1: f64 = 1.2;
const B: f64 = 0.75;

pub struct Bm25Index {
    doc_freq: HashMap<String, usize>,
    term_freqs: HashMap<String, HashMap<String, usize>>,
    avg_doc_len: f64,
    total_docs: usize,
    version: u64,
}

impl Bm25Index {
    pub fn new() -> Self {
        Self {
            doc_freq: HashMap::new(),
            term_freqs: HashMap::new(),
            avg_doc_len: 0.0,
            total_docs: 0,
            version: 0,
        }
    }

    pub fn tokenize(text: &str) -> Vec<String> {
        // CJK detection + bigram, otherwise whitespace + stopword filter
        // Same logic as Python _tokenize()
        todo!("Implement tokenize")
    }

    pub fn rebuild(&mut self, docs: &[(String, String)], new_version: u64) {
        // Rebuild DF table from doc list
        todo!("Implement rebuild")
    }

    pub fn score(&self, query: &str, doc_id: &str) -> f64 {
        // Okapi BM25 score
        todo!("Implement score")
    }

    pub fn version(&self) -> u64 {
        self.version
    }

    pub fn total_docs(&self) -> usize {
        self.total_docs
    }
}
```

- [ ] **Step 2: Wire version check in `supply()`**

```rust
// In ContextEngine.supply(), at entry:
let current_version: u64 = self.storage
    .query_scalar("SELECT version FROM memory_version")
    .unwrap_or(0);
if self.bm25_index.version() != current_version {
    let all_docs = self.storage.list_all_ids_and_content()?;
    self.bm25_index.rebuild(&all_docs, current_version);
}
```

- [ ] **Step 3: Create `memory_version` table if not exists**

Add to `SQL_CREATE_MEMORIES`:
```sql
CREATE TABLE IF NOT EXISTS memory_version (version INTEGER DEFAULT 0);
INSERT OR IGNORE INTO memory_version (version) VALUES (0);
```

- [ ] **Step 4: Build and test**

```bash
cd rust/context-engine-core && cargo build --release && cargo test
```

- [ ] **Step 5: Commit**

```bash
git add rust/context-engine-core/src/
git commit -m "feat(rust): add Bm25Index with version-checked lazy refresh from SQLite"
```

---

### Task 2.4: 13-Stage Pipeline in Rust `supply()`

**Files:**
- Modify: `rust/context-engine-core/src/context_engine.rs` (replace fallback path with full pipeline)
- Modify: `rust/context-engine-core/src/retrieval/mod.rs` (add RRF fusion helper)
- Modify: `rust/context-engine-core/src/retrieval/diversity.rs` (add MMR implementation)

**Pipeline stages to implement:**
0. Adaptive retrieval gate (CJK<6 or EN<15 → return empty)
1. Query expansion (static synonym dict, max 5 terms)
2. Embedding (received from Python via `task_vector` param)
3. Parallel vector + BM25 search
4. RRF fusion (K=60)
5. Min score filter (< 0.3 discard)
6. Rerank (stub — Phase 2 defers to Python for Ollama calls)
7. Recency boost (additive: exp(-ageDays/14) * 0.1)
8. Importance weight (multiplicative: 0.7 + 0.3 * importance)
9. Length normalization (1/(1+0.5*log2(len/500)))
10. Time decay (access-reinforced exponential)
11. Hard min score (< 0.35 discard)
12. MMR diversity (cos>0.85 → score * 0.70)
— Layering (core≥0.70, related≥0.40, divergent≥0.20)

- [ ] **Step 1: Implement each stage as a separate function**

Each stage takes `&[ScoredItem]` (or `Vec<ScoredItem>`) and returns `Vec<ScoredItem>`.

- [ ] **Step 2: Chain stages in `supply()`**

```rust
let mut candidates = self.hybrid_search(&task_vector, &task_description, &scope, &task_type);
candidates = Self::rrf_fuse(candidates);
candidates = Self::min_score_filter(candidates, 0.3);
candidates = self.rerank_stub(candidates);  // no-op for now
candidates = Self::recency_boost(candidates);
candidates = Self::importance_weight(candidates);
candidates = Self::length_normalize(candidates);
candidates = Self::time_decay(candidates);
candidates = Self::hard_min_score(candidates, 0.35);
candidates = Self::mmr_diversity(candidates, 0.85, 0.70);
let pack = Self::layer_results(candidates);
```

- [ ] **Step 3: Build with `cargo build --release`**

All stages must compile. Write 1-2 unit tests per stage.

- [ ] **Step 4: Commit**

```bash
git add rust/context-engine-core/src/
git commit -m "feat(rust): implement 13-stage retrieval pipeline in supply()"
```

---

### Task 2.5: Uncomment Rust Dispatch in Python `supply()`

**Files:**
- Modify: `plastic_promise/core/context_engine.py:supply()` (uncomment Rust dispatch block)

- [ ] **Step 1: Uncomment Rust dispatch**

```python
# In supply(), uncomment lines ~1085-1096:

if self._check_rust_health():
    try:
        return self._supply_rust(
            task_description, task_vector, task_type, scope
        )
    except Exception as e:
        logger.warning(
            "Rust supply failed, falling back to Python: %s", e
        )
        with self._rust_lock:
            self._rust_healthy = None
            self._rust_engine_instance = None

return self._supply_python(task_description, task_vector, task_type, scope)
```

- [ ] **Step 2: Add `PP_FORCE_PYTHON_SUPPLY` gate**

```python
import os as _os
if _os.environ.get("PP_FORCE_PYTHON_SUPPLY", "0") == "1":
    return self._supply_python(task_description, task_vector, task_type, scope)
# ... then Rust dispatch as above
```

- [ ] **Step 3: Update `_supply_rust` to pass version**

```python
def _supply_rust(self, task_description, task_vector, task_type, scope):
    from context_engine_core import ContextEngine as RustEngine
    import tempfile, os as _os

    memories = [...existing code...]
    lancedb_tmp = _os.path.join(tempfile.gettempdir(), "pp_rust_lancedb")
    rust = RustEngine.new_with_backends(
        _os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db"),
        lancedb_tmp
    )
    rust.set_current_time(datetime.datetime.now().isoformat())
    rust_pack = rust.supply(task_description, task_vector, task_type, scope, [])
    return self._convert_rust_pack(rust_pack)
```

Note: `memories` parameter is now `[]` (empty) because Rust reads from its own SQLite connection.

- [ ] **Step 4: Verify fallback chain**

```bash
cd "F:\Agent\Memory system" && PP_FORCE_PYTHON_SUPPLY=1 python -c "
from plastic_promise.core.context_engine import ContextEngine
e = ContextEngine()
pack = e.supply('test', [0.0]*1024, 'general', 'global')
assert '0.1.0-py' in str(pack.audit_metadata.get('engine_version',''))
print('Python fallback: PASS')
"
```

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat(rust-dispatch): uncomment Rust supply path with PP_FORCE_PYTHON_SUPPLY gate"
```

---

### Task 2.6: Activate Domain Models

**Files:**
- Modify: `rust/context-engine-core/src/context_engine.rs` (wire WeibullDecay, WilsonWorth, DefaultTierManager into supply)

Integrate existing domain model implementations into the pipeline:
- `WeibullDecay` → time decay stage (stage 10)
- `WilsonWorthCalculator` → feedback multiplier in scoring
- `DefaultTierManager` → L1 boost in BM25/vector scoring

- [ ] **Step 1: Verify domain model tests pass**

```bash
cd rust/context-engine-core && cargo test --lib domain
```

- [ ] **Step 2: Wire models into pipeline stages**

Replace stub calculations with real domain model calls.

- [ ] **Step 3: Commit**

```bash
git add rust/context-engine-core/src/
git commit -m "feat(rust): activate WeibullDecay, WilsonWorth, DefaultTierManager in supply pipeline"
```

---

### Task 2.7: Preserve Python Fallback

**Files:**
- Modify: `plastic_promise/core/context_engine.py:_supply_python` (add deprecation docstring, no code removal)

- [ ] **Step 1: Add deprecation marker to `_supply_python`**

```python
def _supply_python(self, task_description, task_vector, task_type, scope):
    """供应上下文 — Python 参考实现。
    
    @deprecated: 主路径已迁移到 Rust (_supply_rust)。此方法仅在
    Rust 引擎不可用或 PP_FORCE_PYTHON_SUPPLY=1 时作为 fallback 使用。
    包含 Phase 1 全部改进（BM25 + 长度归一化 + MMR + rerank）。
    """
    # ... existing code unchanged ...
```

- [ ] **Step 2: Verify fallback still works**

```bash
cd "F:\Agent\Memory system" && PP_FORCE_PYTHON_SUPPLY=1 python tests/test_recall_quality.py
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "docs: mark _supply_python as deprecated fallback, preserve Phase 1 improvements"
```

---

## Verification Gates

| Gate | After Task | Command |
|------|-----------|---------|
| No ghost vectors | 1.1 | `python -c "from plastic_promise.core.context_engine import ContextEngine; e=ContextEngine(); e._ensure_heavy_init(); assert e._ldb.count_rows() <= e.memory_count"` |
| BM25 ≥ 20 hits | 1.2 | `python -c "from plastic_promise.core.context_engine import ContextEngine; e=ContextEngine(); assert len(e._text_retrieval('code review')) >= 20"` |
| Thresholds configurable | 1.3 | `PP_CORE_MIN_RELEVANCE=0.5 python -c "from plastic_promise.core.constants import CONTEXT_LAYERS; assert CONTEXT_LAYERS['core']['min_relevance']==0.5"` |
| Phase 1 E2E | 1.7 | `python tests/test_recall_quality.py` (all 7 checks PASS) |
| Rust schema + build | 2.1 | `cd rust/context-engine-core && cargo build --release && cargo test` |
| Rust read-only SQLite | 2.2 | `cargo test` |
| BM25 version check | 2.3 | `cargo test --lib bm25` |
| 13-stage pipeline | 2.4 | `cargo test --lib retrieval` |
| Python fallback chain | 2.5 | `PP_FORCE_PYTHON_SUPPLY=1 python tests/test_recall_quality.py` |
| Domain models active | 2.6 | `cargo test --lib domain` |
| Kendall Tau ≥ 0.90 | 2.7 | Compare Python vs Rust output on same query |
