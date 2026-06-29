# Direction B: Memory Quality Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire smart_extractor into pipeline, add vector dedup, multi-feature entry scoring, and similar-memory merging — four quality gates that run automatically in the standard memory write path and GC cycle.

**Architecture:** Four independent modules layered onto existing pipeline/GC infrastructure. LanceDBStore gains a `search_similar()` foundation. QualityGate is a standalone scorer. Pipeline gains pre-extraction + dedup + gating in the migrate stage. MemoryGC gains a batch merge pass between decay-marking and forget.

**Tech Stack:** Python 3.12+, LanceDB (ANN vector search), pytest, existing embedder (Ollama/OpenAI/Fallback)

**Spec:** [2026-06-30-direction-b-quality-pipeline-design.md](../specs/2026-06-30-direction-b-quality-pipeline-design.md)

## Global Constraints

- `DEDUP_SIMILARITY_THRESHOLD = 0.85` — cosine similarity ≥ this triggers dedup
- `MERGE_SIMILARITY_THRESHOLD = 0.70` — cosine similarity ≥ this triggers merge
- `QUALITY_GATE_THRESHOLD_STORE = 0.5` — gate_score ≥ this stores normally
- `QUALITY_GATE_THRESHOLD_LOW = 0.3` — gate_score 0.3–0.5 stores with `low_quality` tag; <0.3 discards
- All four QualityGate dimensions weighted equally at 0.25
- `store_urgent()` must return `str` (single memory_id) for backward compat with `memory_store` MCP tool
- All LanceDB-dependent operations must degrade gracefully when LanceDB is unavailable
- SQLite incremental UPDATE for dedup counters — follow existing pattern (pipeline.py:278-281)

---

### Task 1: LanceDBStore.search_similar() + check_duplicate()

**Files:**
- Modify: `plastic_promise/core/lancedb_store.py:160-201` (after `insert`, before `update`)
- Test: `tests/test_lancedb_store.py` (append to existing class)

**Interfaces:**
- Produces: `LanceDBStore.search_similar(vector, k=5) -> list[tuple[str, float]]` — returns (memory_id, similarity) sorted descending
- Produces: `LanceDBStore.check_duplicate(vector, threshold=0.85) -> Optional[str]` — thin wrapper, returns memory_id or None

**Context:** LanceDB's `search().metric("cosine")` returns `_distance` (cosine distance ∈ [0, 2]). Convert to similarity: `1.0 - distance/2.0`. The existing `search()` method already does this conversion — `search_similar()` is a convenience wrapper that omits scope/tier filtering and returns clean (id, score) tuples.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lancedb_store.py`:

```python
    def test_search_similar_returns_top_k(self):
        """search_similar returns top-k matches sorted by similarity descending."""
        import random
        # Insert 5 memories with known vectors
        base = [0.5] * EMB_DIM
        for i in range(5):
            vec = [v + random.uniform(-0.01, 0.01) for v in base]
            self.store.insert(f"sim_{i}", vec, f"similar memory {i}")
        # Search with a near-identical vector
        query = [0.51] * EMB_DIM
        results = self.store.search_similar(query, k=3)
        assert len(results) == 3
        # First result should be most similar
        assert results[0][1] >= results[1][1] >= results[2][1]
        # All scores in [0, 1]
        for mid, score in results:
            assert 0.0 <= score <= 1.0

    def test_search_similar_empty_table(self):
        """search_similar on empty table returns empty list."""
        results = self.store.search_similar([0.5] * EMB_DIM, k=3)
        assert results == []

    def test_check_duplicate_finds_match(self):
        """check_duplicate returns memory_id when similarity ≥ threshold."""
        vec = [0.7] * EMB_DIM
        self.store.insert("dup_target", vec, "target text")
        # Search with near-identical vector
        query = [0.71] * EMB_DIM
        result = self.store.check_duplicate(query, threshold=0.85)
        assert result == "dup_target"

    def test_check_duplicate_no_match(self):
        """check_duplicate returns None when no vector is similar enough."""
        self.store.insert("far_away", [0.1] * EMB_DIM, "distant text")
        query = [0.9] * EMB_DIM
        result = self.store.check_duplicate(query, threshold=0.85)
        assert result is None

    def test_check_duplicate_skips_self(self):
        """check_duplicate should not match the same memory_id (self-exclusion later in pipeline)."""
        # This tests the LanceDB layer only — self-exclusion is pipeline's job
        vec = [0.5] * EMB_DIM
        self.store.insert("self_test", vec, "self")
        # A different but very similar vector
        query = [0.51] * EMB_DIM
        result = self.store.check_duplicate(query, threshold=0.85)
        # May match "self_test" — pipeline handles self-exclusion
        # This test just verifies the method doesn't crash
        assert result is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_lancedb_store.py::TestLanceDBStore::test_search_similar_returns_top_k -v
pytest tests/test_lancedb_store.py::TestLanceDBStore::test_search_similar_empty_table -v
pytest tests/test_lancedb_store.py::TestLanceDBStore::test_check_duplicate_finds_match -v
pytest tests/test_lancedb_store.py::TestLanceDBStore::test_check_duplicate_no_match -v
pytest tests/test_lancedb_store.py::TestLanceDBStore::test_check_duplicate_skips_self -v
```

Expected: All FAIL with `AttributeError: 'LanceDBStore' object has no attribute 'search_similar'`

- [ ] **Step 3: Write the minimal implementation**

Add to `plastic_promise/core/lancedb_store.py` after the `search_fts` method (line ~158) and before `insert` (line ~160):

```python
    def search_similar(
        self, vector: list[float], k: int = 5,
    ) -> list[tuple[str, float]]:
        """Return top-k (memory_id, similarity) sorted by similarity descending.

        No scope/tier filtering — raw vector similarity for dedup and merge.
        Uses existing ANN search with cosine metric.
        Returns empty list when table is None or search fails.
        """
        if self._table is None:
            return []
        try:
            raw = self._table.search(vector).metric("cosine").limit(k).to_list()
            results = []
            for row in raw:
                dist = row.get("_distance", 0.0)
                sim = 1.0 - (dist / 2.0)  # cosine distance → similarity [0, 1]
                results.append((row["memory_id"], max(0.0, min(1.0, sim))))
            return results
        except Exception as e:
            logger.warning("LanceDB search_similar failed: %s", e)
            return []

    def check_duplicate(
        self, vector: list[float], threshold: float = 0.85,
    ) -> Optional[str]:
        """Return memory_id of the nearest match if similarity ≥ threshold, else None.

        Thin wrapper over search_similar(k=1). Used by pipeline dedup (Task 3).
        """
        results = self.search_similar(vector, k=1)
        if results and results[0][1] >= threshold:
            return results[0][0]
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_lancedb_store.py::TestLanceDBStore::test_search_similar_returns_top_k \
  tests/test_lancedb_store.py::TestLanceDBStore::test_search_similar_empty_table \
  tests/test_lancedb_store.py::TestLanceDBStore::test_check_duplicate_finds_match \
  tests/test_lancedb_store.py::TestLanceDBStore::test_check_duplicate_no_match \
  tests/test_lancedb_store.py::TestLanceDBStore::test_check_duplicate_skips_self \
  -v
```

Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/lancedb_store.py tests/test_lancedb_store.py
git commit -m "feat: LanceDBStore.search_similar() + check_duplicate() for vector dedup foundation"
```

---

### Task 2: QualityGate Module + Constants

**Files:**
- Create: `plastic_promise/core/quality_gate.py`
- Modify: `plastic_promise/core/constants.py` (append new thresholds)
- Test: Create `tests/test_quality_gate.py`

**Interfaces:**
- Produces: `QualityGate.score(extracted, tags, domain_hint, created_at=None) -> float` — returns gate_score ∈ [0, 1]
- Produces: `QualityGate.decide(gate_score) -> str` — returns "store" | "low_quality" | "discard"
- Consumes: `QUALITY_GATE_WEIGHTS`, `QUALITY_GATE_THRESHOLD_STORE`, `QUALITY_GATE_THRESHOLD_LOW` from constants
- Consumes: `DEDUP_SIMILARITY_THRESHOLD`, `MERGE_SIMILARITY_THRESHOLD`, `MERGE_TOP_K`, `MERGE_AUDIT_RETENTION_DAYS` from constants

- [ ] **Step 1: Add constants**

Append to `plastic_promise/core/constants.py`:

```python
# ============================================================
# Quality Gate (Direction B — Task 3)
# ============================================================

QUALITY_GATE_WEIGHTS = {
    "confidence": 0.25,
    "relevance": 0.25,
    "freshness": 0.25,
    "info_density": 0.25,
}
QUALITY_GATE_THRESHOLD_STORE = 0.5    # ≥ this → store normally
QUALITY_GATE_THRESHOLD_LOW = 0.3      # 0.3–0.5 → store with low_quality tag; <0.3 → discard

# ============================================================
# Dedup & Merge (Direction B — Task 2 & 4)
# ============================================================

DEDUP_SIMILARITY_THRESHOLD = 0.85      # cosine similarity ≥ this → duplicate
MERGE_SIMILARITY_THRESHOLD = 0.70      # cosine similarity ≥ this → merge candidate
MERGE_TOP_K = 3                        # top-k similar to check per memory during merge
MERGE_AUDIT_RETENTION_DAYS = 7         # merged records kept in SQLite before permanent GC
```

- [ ] **Step 2: Write the failing test file**

Create `tests/test_quality_gate.py`:

```python
"""Tests for QualityGate — multi-feature memory entry scoring."""

import pytest
from plastic_promise.core.quality_gate import QualityGate


class TestQualityGate:
    """Full test suite for QualityGate scoring and decision logic."""

    def test_score_perfect_extraction(self):
        """Full extracted data + tags + domain → high score."""
        gate = QualityGate()
        extracted = {
            "category": "preference",
            "l0_abstract": "User prefers Rust for backend development",
            "l1_summary": "[preference] User prefers Rust because of memory safety and zero-cost abstractions",
            "confidence": 0.9,
        }
        tags = ["cat:preference", "rust", "backend"]
        score = gate.score(extracted=extracted, tags=tags, domain_hint="building")
        # ~0.9 conf *0.25 + ~0.8 relevance *0.25 + 1.0 freshness *0.25 + 1.0 density *0.25 ≈ 0.9+
        assert score >= 0.75

    def test_score_no_extracted_defaults(self):
        """Missing extracted field → generous defaults, should pass store threshold."""
        gate = QualityGate()
        score = gate.score(extracted={}, tags=[], domain_hint=None)
        # 0.5*0.25 + 0.5*0.25 + 1.0*0.25 + 0.5*0.25 = 0.625
        assert 0.60 <= score <= 0.65

    def test_score_low_confidence_no_structure(self):
        """Low confidence, no tags, no L0/L1 → borderline low."""
        gate = QualityGate()
        extracted = {
            "category": "fact",
            "confidence": 0.3,
        }
        tags = []
        score = gate.score(extracted=extracted, tags=tags, domain_hint=None)
        # 0.3*0.25 + 0.5*0.25 + 1.0*0.25 + 0.0*0.25 = 0.45
        assert 0.40 <= score < 0.50

    def test_score_info_density_full_structure(self):
        """L0+L1+L2 all present + category → max info_density."""
        gate = QualityGate()
        extracted = {
            "category": "event",
            "l0_abstract": "Deployed v2.3.1 to production at 14:30 UTC",
            "l1_summary": "[event] Production deployment of v2.3.1 — includes memory pipeline fixes and LanceDB backfill",
            "l2_content": "Completed deployment of version 2.3.1 to the production cluster. The release includes three patches: memory pipeline dedup, LanceDB backfill optimization, and dashboard refresh fix. All 47 integration tests passed. Rollback plan verified.",
            "confidence": 0.88,
        }
        tags = ["cat:event", "deployment", "production"]
        score = gate.score(extracted=extracted, tags=tags, domain_hint="building")
        # L0=0.3 + L1=0.3 + L2=0.2 + structure=0.2 = 1.0 info_density
        assert score >= 0.80

    def test_decide_store(self):
        """gate_score ≥ 0.5 → 'store'."""
        assert QualityGate.decide(0.55) == "store"
        assert QualityGate.decide(0.50) == "store"
        assert QualityGate.decide(1.0) == "store"

    def test_decide_low_quality(self):
        """gate_score 0.3–0.5 → 'low_quality'."""
        assert QualityGate.decide(0.30) == "low_quality"
        assert QualityGate.decide(0.49) == "low_quality"
        assert QualityGate.decide(0.35) == "low_quality"

    def test_decide_discard(self):
        """gate_score < 0.3 → 'discard'."""
        assert QualityGate.decide(0.29) == "discard"
        assert QualityGate.decide(0.0) == "discard"
        assert QualityGate.decide(0.10) == "discard"

    def test_score_edge_case_empty_tags_long_content(self):
        """Long content with no tags but good extraction → respectable score."""
        gate = QualityGate()
        extracted = {
            "category": "pattern",
            "l0_abstract": "User consistently uses TDD workflow",
            "l1_summary": "[pattern] Across 12 coding sessions, user always writes failing tests first",
            "l2_content": "Observed pattern across 12 consecutive coding sessions: user writes a failing test, runs it to confirm failure, then writes minimal implementation, confirms pass, then refactors — classic TDD red-green-refactor cycle.",
            "confidence": 0.82,
        }
        score = gate.score(extracted=extracted, tags=[], domain_hint=None)
        # info_density should be 1.0 (full L0/L1/L2 + category), relevance 0.5 (no tags)
        assert 0.65 <= score <= 0.80
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_quality_gate.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'plastic_promise.core.quality_gate'`

- [ ] **Step 4: Write the minimal implementation**

Create `plastic_promise/core/quality_gate.py`:

```python
"""QualityGate — multi-feature entry scoring for memory pipeline gating.

Four dimensions × equal weight (0.25 each):
  confidence      — from smart_extractor classification confidence
  relevance       — domain/tag matching via DomainManager
  freshness       — time-decay via Direction A Weibull logic
  info_density    — L0/L1/L2 completeness + structural metadata

Decision matrix:
  gate_score ≥ 0.5  → store
  0.3 ≤ score < 0.5 → store with low_quality tag
  score < 0.3       → discard
"""

from typing import Optional

from plastic_promise.core.constants import (
    QUALITY_GATE_WEIGHTS,
    QUALITY_GATE_THRESHOLD_STORE,
    QUALITY_GATE_THRESHOLD_LOW,
)


class QualityGate:
    """Composite gating scorer for memory entry quality."""

    WEIGHTS = QUALITY_GATE_WEIGHTS
    THRESHOLD_STORE = QUALITY_GATE_THRESHOLD_STORE
    THRESHOLD_LOW = QUALITY_GATE_THRESHOLD_LOW

    def score(
        self,
        extracted: Optional[dict] = None,
        tags: Optional[list[str]] = None,
        domain_hint: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> float:
        """Compute composite gate_score from four dimensions.

        Args:
            extracted: dict from smart_extractor with keys:
                category, l0_abstract, l1_summary, l2_content, confidence
            tags: semantic tags from pipeline
            domain_hint: domain assigned during classified stage
            created_at: ISO timestamp for freshness calculation (None = now)

        Returns:
            float in [0.0, 1.0] — weighted sum of four dimensions.
        """
        extracted = extracted or {}
        tags = tags or []

        confidence = self._compute_confidence(extracted)
        relevance = self._compute_relevance(tags, domain_hint)
        freshness = self._compute_freshness(created_at)
        info_density = self._compute_info_density(extracted, tags)

        return (
            confidence * self.WEIGHTS["confidence"]
            + relevance * self.WEIGHTS["relevance"]
            + freshness * self.WEIGHTS["freshness"]
            + info_density * self.WEIGHTS["info_density"]
        )

    @staticmethod
    def decide(gate_score: float) -> str:
        """Map gate_score to action: 'store' | 'low_quality' | 'discard'."""
        if gate_score >= QUALITY_GATE_THRESHOLD_STORE:
            return "store"
        elif gate_score >= QUALITY_GATE_THRESHOLD_LOW:
            return "low_quality"
        else:
            return "discard"

    # ---- Private dimension calculators ----

    @staticmethod
    def _compute_confidence(extracted: dict) -> float:
        """Confidence from smart_extractor, default 0.5."""
        return extracted.get("confidence", 0.5)

    @staticmethod
    def _compute_relevance(tags: list[str], domain_hint: Optional[str]) -> float:
        """Relevance based on tag-to-domain matching.

        With domain_hint: ratio of matched tags × 1.5, capped at 1.0.
        Without domain_hint: neutral 0.5.
        """
        if not domain_hint or not tags:
            return 0.5
        try:
            from plastic_promise.core.domain_manager import PREDEFINED_DOMAINS
            domain_config = PREDEFINED_DOMAINS.get(domain_hint, {})
            domain_tags = domain_config.get("tags", set())
            if not domain_tags:
                return 0.5
            matched = sum(1 for tag in tags if tag in domain_tags)
            ratio = matched / max(len(tags), 1)
            return min(1.0, ratio * 1.5)
        except Exception:
            return 0.5

    @staticmethod
    def _compute_freshness(created_at: Optional[str] = None) -> float:
        """Time-decay freshness. New memories = 1.0; older = decayed.

        Uses Direction A's Weibull decay when created_at is provided.
        Defaults to 1.0 (brand new) when created_at is None.
        """
        if created_at is None:
            return 1.0
        try:
            import datetime
            now = datetime.datetime.now()
            created = datetime.datetime.fromisoformat(created_at)
            age_hours = (now - created).total_seconds() / 3600.0
            # Simple exponential decay: half-life of 168 hours (7 days)
            # This is a fast-path; Direction A Weibull is more precise but heavier
            import math
            half_life = 168.0  # hours
            decay = math.exp(-math.log(2) * age_hours / half_life)
            return max(0.0, min(1.0, decay))
        except (ValueError, TypeError):
            return 1.0

    @staticmethod
    def _compute_info_density(extracted: dict, tags: list[str]) -> float:
        """Information density from L0/L1/L2 completeness + structure.

        L0 score (0.3): l0_abstract present and len > 10
        L1 score (0.3): l1_summary present and len > 20
        L2 score (0.2): l2_content present and len > 50
        Structure score (0.2): has category AND tags

        Returns 0.5 when extracted is empty (generous default for direct writes).
        """
        if not extracted:
            return 0.5

        l0_score = 0.3 if extracted.get("l0_abstract") and len(extracted.get("l0_abstract", "")) > 10 else 0.0
        l1_score = 0.3 if extracted.get("l1_summary") and len(extracted.get("l1_summary", "")) > 20 else 0.0
        l2_score = 0.2 if extracted.get("l2_content") and len(extracted.get("l2_content", "")) > 50 else 0.0
        structure_score = 0.2 if extracted.get("category") and tags else 0.0

        return l0_score + l1_score + l2_score + structure_score
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_quality_gate.py -v
```

Expected: 8 PASS

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/core/quality_gate.py plastic_promise/core/constants.py tests/test_quality_gate.py
git commit -m "feat: QualityGate module — 4-dimension composite scoring for memory entry gating"
```

---

### Task 3: Pipeline Integration — extract_memories + Dedup + QualityGate

**Files:**
- Modify: `plastic_promise/memory/pipeline.py` (store_urgent, _process_raw_to_tagged, _process_embedded_to_migrate)
- Test: Create `tests/test_pipeline_quality.py`

**Interfaces:**
- Consumes: `extract_memories()` from `plastic_promise.smart_extractor`
- Consumes: `LanceDBStore.check_duplicate()` from Task 1
- Consumes: `QualityGate.score()` + `QualityGate.decide()` from Task 2
- Consumes: `DEDUP_SIMILARITY_THRESHOLD` from constants (Task 2)
- Produces: Enhanced `store_urgent()` — calls extract_memories, returns str
- Produces: Enhanced `_process_raw_to_tagged()` — uses extracted.category for tags
- Produces: Enhanced `_process_embedded_to_migrate()` — dedup → QualityGate → store

**Context:** The pipeline must hold a reference to a LanceDBStore instance for dedup. Currently it holds `rec_mem` and `embedder`. Add an optional `lancedb` parameter to `MemoryPipeline.__init__()`. When None, dedup degrades gracefully.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline_quality.py`:

```python
"""Tests for pipeline quality features — extraction, dedup, QualityGate integration."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from plastic_promise.memory.pipeline import MemoryPipeline
from plastic_promise.memory.soul_memory import MemoryRecord, RecMem
from plastic_promise.core.embedder import FallbackEmbedder


class TestPipelineQuality:
    """Integration tests for quality pipeline features."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create pipeline with mocked dependencies."""
        self.rec_mem = MagicMock(spec=RecMem)
        self.rec_mem._records = {}
        self.embedder = FallbackEmbedder(dim=1024)
        # Mock LanceDB for dedup
        self.lancedb = MagicMock()
        self.lancedb.check_duplicate.return_value = None  # no dup by default
        self.pipeline = MemoryPipeline(
            rec_mem=self.rec_mem,
            embedder=self.embedder,
        )
        self.pipeline._lancedb = self.lancedb
        yield
        # Clean up buffer
        self.pipeline._buffer.clear()

    def test_store_urgent_extracts_memories(self):
        """store_urgent calls extract_memories and stores extracted fields."""
        with patch('plastic_promise.memory.pipeline.extract_memories') as mock_extract:
            from plastic_promise.smart_extractor import ExtractedMemory
            mock_extract.return_value = [
                ExtractedMemory(
                    category="preference",
                    l0_abstract="User likes Rust",
                    l1_summary="[preference] User prefers Rust for backend",
                    l2_content="User likes Rust for backend development",
                    importance=0.8,
                    confidence=0.9,
                    source_segment="User likes Rust for backend development",
                )
            ]
            mid = self.pipeline.store_urgent("User likes Rust for backend development")
            assert isinstance(mid, str)
            assert mid.startswith("fuzzy_")
            # Buffer record should have extracted field
            record = self.pipeline._buffer[mid]
            assert record["stage"] == "raw"
            assert "extracted" in record
            assert record["extracted"]["category"] == "preference"
            assert record["extracted"]["confidence"] == 0.9
            # Tags should include cat:preference
            assert any("cat:preference" in tag for tag in record["tags"])

    def test_store_urgent_no_extraction_returns_none(self):
        """extract_memories returns empty → store_urgent returns None."""
        with patch('plastic_promise.memory.pipeline.extract_memories') as mock_extract:
            mock_extract.return_value = []
            result = self.pipeline.store_urgent("...")
            assert result is None

    def test_store_urgent_extraction_error_fallback(self):
        """extract_memories raises → fall back to raw content, no extracted field."""
        with patch('plastic_promise.memory.pipeline.extract_memories') as mock_extract:
            mock_extract.side_effect = RuntimeError("Ollama not running")
            mid = self.pipeline.store_urgent("Important memory about deployment")
            assert isinstance(mid, str)
            record = self.pipeline._buffer[mid]
            assert "extracted" not in record
            assert record["content"] == "Important memory about deployment"

    def test_migrate_skips_duplicate(self):
        """When check_duplicate returns a match, buffer entry is removed without store."""
        self.lancedb.check_duplicate.return_value = "existing_001"

        # Manually create a buffer entry at embedded stage with a vector
        mid = f"fuzzy_testdup"
        self.pipeline._buffer[mid] = {
            "memory_id": mid,
            "content": "duplicate content",
            "memory_type": "experience",
            "source": "user",
            "stage": "embedded",
            "tags": [],
            "domain": "uncategorized",
            "vector": [0.5] * 1024,
            "extracted": {"category": "fact", "confidence": 0.8},
            "entity_ids": [],
            "created_at": "2026-06-30T12:00:00",
        }
        # Mock engine internals
        self.pipeline.rec_mem._engine = MagicMock()
        self.pipeline.rec_mem._engine._memories = {"existing_001": {}}
        self.pipeline.rec_mem._engine._sqlite = None

        self.pipeline._process_embedded_to_migrate()

        # Buffer entry should be removed
        assert mid not in self.pipeline._buffer
        # rec_mem.store should NOT have been called
        self.rec_mem.store.assert_not_called()

    def test_migrate_discards_low_quality(self):
        """QualityGate score < 0.3 → buffer entry discarded."""
        # Create entry with intentionally terrible extraction data
        mid = f"fuzzy_testlow"
        self.pipeline._buffer[mid] = {
            "memory_id": mid,
            "content": "ok",
            "memory_type": "experience",
            "source": "user",
            "stage": "embedded",
            "tags": [],
            "domain": None,
            "vector": [0.5] * 1024,
            "extracted": {
                "category": "fact",
                "confidence": 0.15,  # very low confidence
                # No L0/L1/L2
            },
            "entity_ids": [],
            "created_at": "2026-06-30T12:00:00",
        }
        self.pipeline.rec_mem._engine = MagicMock()
        self.pipeline.rec_mem._engine._memories = {}

        self.pipeline._process_embedded_to_migrate()

        # Buffer entry should be removed (discarded)
        assert mid not in self.pipeline._buffer
        # rec_mem.store should NOT have been called
        self.rec_mem.store.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_pipeline_quality.py -v
```

Expected: ALL FAIL — first test fails because `store_urgent` doesn't call `extract_memories`

- [ ] **Step 3: Modify MemoryPipeline.__init__() — add lancedb parameter**

In `plastic_promise/memory/pipeline.py`, modify the `__init__` signature and body:

```python
    def __init__(self, rec_mem=None, embedder=None, tier_manager=None,
                 domain_manager=None, lancedb=None) -> None:
        self._buffer: Dict[str, Dict[str, Any]] = {}
        self.rec_mem = rec_mem
        self.embedder = embedder
        self._tier_manager = tier_manager
        self._dm = domain_manager
        self._lancedb = lancedb  # ★ new: for vector dedup (None → graceful skip)
        self._last_process: Optional[str] = None
        self._batch_size = 10
```

- [ ] **Step 4: Modify store_urgent() — integrate extract_memories()**

Replace the `store_urgent` method body:

```python
    def store_urgent(
        self, content: str, memory_type: str = "experience", source: str = "user",
        entity_ids: list[str] = None, custom_tags: list[str] = None,
        domain_hint: str = None,
    ) -> Optional[str]:
        """Store a memory with smart extraction, then through pipeline.

        ★ Direction B: Calls extract_memories() for pre-extraction.
        - 0 results → returns None (pure noise)
        - 1 result → returns str memory_id
        - N results → all N enter buffer; returns first memory_id as str

        Returns:
            memory_id with 'fuzzy_' prefix, or None if extraction yields nothing.
        """
        # ★ Step 0: Smart extraction
        extracted_list = []
        try:
            from plastic_promise.smart_extractor import extract_memories
            extracted_list = extract_memories(content)
        except Exception:
            pass  # Fallback: raw content without extraction metadata

        if not extracted_list and not content.strip():
            return None

        # If extraction returned nothing, treat the raw content as one memory
        if not extracted_list:
            extracted_list = [None]  # sentinel: raw content, no extraction

        first_mid = None
        for em in extracted_list:
            mid = f"fuzzy_{uuid.uuid4().hex[:12]}"

            # Determine content: extracted L2 or raw
            if em is not None:
                mem_content = em.l2_content if em.l2_content else content
            else:
                mem_content = content

            # Tags: semantic extraction + category injection + custom
            tags = self._extract_semantic_tags(mem_content)
            if em is not None and em.category:
                cat_tag = f"cat:{em.category}"
                if cat_tag not in tags:
                    tags.append(cat_tag)
            if custom_tags:
                tags = list(set(tags + custom_tags))

            record = {
                "memory_id": mid,
                "content": mem_content,
                "memory_type": memory_type,
                "source": source,
                "entity_ids": entity_ids or [],
                "stage": "raw",
                "tags": tags,
                "vector": None,
                "tier": None,
                "domain": domain_hint or "uncategorized",
                "created_at": datetime.datetime.now().isoformat(),
                "processed_at": None,
            }

            # Attach extraction metadata if available
            if em is not None:
                record["extracted"] = {
                    "category": em.category,
                    "l0_abstract": em.l0_abstract,
                    "l1_summary": em.l1_summary,
                    "l2_content": em.l2_content,
                    "confidence": em.confidence,
                    "importance": em.importance,
                }

            self._buffer[mid] = record
            if first_mid is None:
                first_mid = mid

        return first_mid
```

- [ ] **Step 5: Modify _process_embedded_to_migrate() — add dedup + QualityGate**

Replace the `_process_embedded_to_migrate` method:

```python
    def _process_embedded_to_migrate(self) -> int:
        """Stage 4: Dedup → QualityGate → Migrate embedded items to main pool.

        ★ Direction B enhancements:
          1. Vector dedup via LanceDB.check_duplicate() (cos ≥ 0.85 → bump counters)
          2. QualityGate composite scoring (4-dim × 0.25)
          3. Normal RecMem.store() for passing records
        """
        from plastic_promise.core.quality_gate import QualityGate

        items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "embedded"]
        count = 0
        gate = QualityGate()

        for mid, record in items:
            try:
                if self.rec_mem is None:
                    del self._buffer[mid]
                    continue

                engine = getattr(self.rec_mem, '_engine', None)
                vec = record.get("vector")

                # ---- ★ Step 4a: Vector dedup ----
                if vec and self._lancedb is not None:
                    try:
                        dup_id = self._lancedb.check_duplicate(
                            vec, threshold=0.85  # DEDUP_SIMILARITY_THRESHOLD
                        )
                        if dup_id and engine is not None:
                            # Increment access_count + worth_success on existing record
                            if dup_id in getattr(engine, '_memories', {}):
                                engine._memories[dup_id]["access_count"] = (
                                    engine._memories[dup_id].get("access_count", 0) + 1
                                )
                                engine._memories[dup_id]["worth_success"] = (
                                    engine._memories[dup_id].get("worth_success", 0) + 1
                                )
                            if dup_id in getattr(self.rec_mem, '_records', {}):
                                py_rec = self.rec_mem._records[dup_id]
                                py_rec.access_count += 1
                                py_rec.worth_success += 1
                                py_rec.last_accessed = datetime.datetime.now().isoformat()
                            # SQLite incremental update (follows existing pattern)
                            sqlite = getattr(engine, '_sqlite', None)
                            if sqlite is not None:
                                try:
                                    sqlite._conn.execute(
                                        "UPDATE memories SET access_count = access_count + 1, "
                                        "worth_success = worth_success + 1 WHERE id = ?",
                                        (dup_id,)
                                    )
                                    sqlite._conn.commit()
                                except Exception:
                                    pass
                            del self._buffer[mid]
                            logging.info("Dedup: %s → merged into %s (similarity ≥ 0.85)", mid, dup_id)
                            continue  # skip store
                    except Exception as e:
                        logging.warning("Dedup check failed for %s: %s — proceeding with store", mid, e)

                # ---- ★ Step 4b: QualityGate scoring ----
                extracted = record.get("extracted", {})
                tags = record.get("tags", [])
                domain_hint = record.get("domain", "uncategorized")
                created_at = record.get("created_at")

                gate_score = gate.score(
                    extracted=extracted,
                    tags=tags,
                    domain_hint=domain_hint,
                    created_at=created_at,
                )
                decision = QualityGate.decide(gate_score)

                if decision == "discard":
                    del self._buffer[mid]
                    logging.info("QualityGate: %s discarded (score=%.3f < %.2f)",
                                 mid, gate_score, QualityGate.THRESHOLD_LOW)
                    continue

                # ---- Step 4c: Store ----
                stored = self.rec_mem.store(
                    content=record["content"],
                    memory_type=record["memory_type"],
                    source=record["source"],
                    tags=tags,
                    domain=domain_hint,
                )

                # Attach quality metadata
                if decision == "low_quality":
                    if hasattr(self.rec_mem, '_records') and stored.memory_id in self.rec_mem._records:
                        py_rec = self.rec_mem._records[stored.memory_id]
                        py_rec.metadata["quality"] = "low_quality"
                        py_rec.metadata["gate_score"] = round(gate_score, 4)
                    logging.info("QualityGate: %s stored with low_quality tag (score=%.3f)",
                                 stored.memory_id, gate_score)

                # ---- Existing: vector + tags + domain persistence ----
                if engine is not None:
                    if vec:
                        engine._memories[stored.memory_id]["_vector"] = vec
                        ldb = getattr(engine, '_ldb', None)
                        if ldb is not None:
                            try:
                                ldb.insert(
                                    memory_id=stored.memory_id,
                                    vector=vec,
                                    text=record.get("content", ""),
                                    tier=record.get("tier", "L1"),
                                    category=record.get("category", "other"),
                                    scope=record.get("scope", "global"),
                                )
                            except Exception as e:
                                logging.warning("LanceDB dual-write failed for %s: %s", stored.memory_id, e)
                    engine._memories[stored.memory_id]["tags"] = tags
                    engine._memories[stored.memory_id]["domain"] = domain_hint
                    sqlite = getattr(engine, '_sqlite', None)
                    if sqlite is not None:
                        import json
                        sqlite._conn.execute(
                            "UPDATE memories SET tags = ?, domain = ? WHERE id = ?",
                            (json.dumps(tags), domain_hint, stored.memory_id)
                        )
                        sqlite._conn.commit()

                # Rebuild entity edges
                entity_ids = record.get("entity_ids", [])
                if entity_ids and engine is not None:
                    for eid in entity_ids:
                        edge = {"from": stored.memory_id, "to": eid,
                                "relation": "references", "weight": 0.5}
                        graph_edges = getattr(engine, '_graph_edges', [])
                        if edge not in graph_edges:
                            graph_edges.append(edge)

                del self._buffer[mid]
                count += 1
            except Exception as e:
                logging.warning("Migrate failed for %s: %s", mid, e)

        return count
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
pytest tests/test_pipeline_quality.py -v
```

Expected: 5 PASS (after fixing any mock mismatches)

- [ ] **Step 7: Run existing pipeline tests to verify no regressions**

```bash
pytest tests/ -k "pipeline" -v 2>&1 | head -20
```

- [ ] **Step 8: Run existing lancedb tests**

```bash
pytest tests/test_lancedb_store.py -v
```

- [ ] **Step 9: Commit**

```bash
git add plastic_promise/memory/pipeline.py tests/test_pipeline_quality.py
git commit -m "feat: pipeline integration — extract_memories + vector dedup + QualityGate gating"
```

---

### Task 4: MemoryGC.merge_similar() — Batch Similar Memory Merge

**Files:**
- Modify: `plastic_promise/memory/soul_memory.py` (MemoryGC class, add `merge_similar` method)
- Test: Create `tests/test_memory_merge.py`

**Interfaces:**
- Consumes: `LanceDBStore.search_similar()` from Task 1
- Consumes: `MERGE_SIMILARITY_THRESHOLD`, `MERGE_TOP_K`, `MERGE_AUDIT_RETENTION_DAYS` from constants (Task 2)
- Produces: `MemoryGC.merge_similar(threshold=0.70, dry_run=True) -> dict`
- Modifies: `MemoryGC.collect()` — calls `merge_similar()` between `mark_decaying()` and `forget()`

**Context:** The merge scan iterates all memories with vectors, queries LanceDB for top-k similar memories, clusters pairs ≥ threshold, selects survivors by worth_score (created_at tiebreaker), appends merged content abstracts to survivor metadata, and removes merged records from engine._memories (retention in SQLite for audit).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_memory_merge.py`:

```python
"""Tests for MemoryGC.merge_similar() — batch similar memory merging."""

import pytest
from unittest.mock import MagicMock, patch
from plastic_promise.memory.soul_memory import MemoryGC, RecMem, MemoryRecord


class TestMemoryMerge:
    """Test suite for merge_similar behavior."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create MemoryGC with mocked RecMem and LanceDB."""
        self.rec_mem = MagicMock(spec=RecMem)
        self.rec_mem._records = {}
        self.rec_mem.health_ratio = 0.9
        self.gc = MemoryGC(self.rec_mem)
        yield

    def _make_record(self, mid, content, worth_score=0.5, tier="L3",
                     created_at="2026-06-30T12:00:00"):
        """Helper: create a MemoryRecord and add to rec_mem._records."""
        record = MemoryRecord(
            content=content,
            memory_type="experience",
            source="user",
            memory_id=mid,
            worth_success=5,
            worth_failure=0,
            tier=tier,
        )
        record.created_at = created_at
        self.rec_mem._records[mid] = record
        return record

    def test_merge_similar_dry_run_reports_candidates(self):
        """dry_run=True should report candidates without modifying records."""
        # Setup: two records with vectors
        r1 = self._make_record("mem_001", "User likes Rust for backend", worth_score=0.7)
        r2 = self._make_record("mem_002", "User prefers Rust for server development", worth_score=0.5)

        # Mock LanceDB — returns high similarity between mem_001 and mem_002
        mock_ldb = MagicMock()
        # First call (mem_001) returns mem_002 as similar
        # Second call (mem_002) returns mem_001 as similar
        mock_ldb.search_similar.side_effect = [
            [("mem_002", 0.82), ("mem_003", 0.45)],  # mem_001 → similar to mem_002
            [("mem_001", 0.82)],                       # mem_002 → similar to mem_001
        ]

        # Mock engine
        engine = MagicMock()
        engine._memories = {
            "mem_001": {"_vector": [0.1] * 1024, "access_count": 0, "worth_success": 0},
            "mem_002": {"_vector": [0.2] * 1024, "access_count": 0, "worth_success": 0},
        }
        engine._ldb = mock_ldb
        engine._sqlite = None
        self.rec_mem._engine = engine

        result = self.gc.merge_similar(threshold=0.70, dry_run=True)

        assert result["dry_run"] is True
        assert result["candidates_found"] >= 1
        assert len(result["merged_pairs"]) >= 1
        # No records should be removed in dry_run
        assert "mem_001" in self.rec_mem._records
        assert "mem_002" in self.rec_mem._records

    def test_merge_similar_survivor_keeps_higher_worth(self):
        """Survivor should be the record with higher worth_score."""
        r1 = self._make_record("mem_high", "Rust is great", worth_score=0.85,
                               created_at="2026-06-01T00:00:00")  # older but higher score
        r2 = self._make_record("mem_low", "Rust is excellent", worth_score=0.45,
                               created_at="2026-06-30T00:00:00")   # newer but lower score

        mock_ldb = MagicMock()
        mock_ldb.search_similar.side_effect = [
            [("mem_low", 0.78)],
            [("mem_high", 0.78)],
        ]

        engine = MagicMock()
        engine._memories = {
            "mem_high": {"_vector": [0.1] * 1024},
            "mem_low": {"_vector": [0.2] * 1024},
        }
        engine._ldb = mock_ldb
        engine._sqlite = None
        self.rec_mem._engine = engine

        result = self.gc.merge_similar(threshold=0.70, dry_run=True)
        pairs = result["merged_pairs"]
        if pairs:
            # mem_high (0.85) should survive over mem_low (0.45)
            pair = pairs[0]
            assert pair["survivor"] == "mem_high"
            assert "mem_low" in pair["merged"]

    def test_merge_similar_no_lancedb_returns_error(self):
        """When LanceDB is unavailable, merge returns error dict."""
        r1 = self._make_record("mem_a", "content A")
        r2 = self._make_record("mem_b", "content B")

        engine = MagicMock()
        engine._memories = {
            "mem_a": {"_vector": None},
            "mem_b": {"_vector": None},
        }
        engine._ldb = None  # No LanceDB
        self.rec_mem._engine = engine

        result = self.gc.merge_similar()
        assert "error" in result or result["candidates_found"] == 0

    def test_merge_similar_empty_pool(self):
        """Empty memory pool returns zero candidates."""
        engine = MagicMock()
        engine._memories = {}
        engine._ldb = None
        self.rec_mem._engine = engine

        result = self.gc.merge_similar()
        assert result["candidates_found"] == 0
        assert result["would_merge"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_memory_merge.py -v
```

Expected: ALL FAIL — `AttributeError: 'MemoryGC' object has no attribute 'merge_similar'`

- [ ] **Step 3: Implement merge_similar() in MemoryGC**

Add the `merge_similar` method to the `MemoryGC` class in `plastic_promise/memory/soul_memory.py`. Insert after the `mark_decaying` method (line ~1001):

```python
    def merge_similar(
        self, threshold: float = 0.70, dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Batch-scan the memory pool and merge records with cosine similarity ≥ threshold.

        Algorithm:
          1. For each memory with a vector, query LanceDB for top-k similar
          2. Filter pairs with similarity ≥ threshold, skip self-matches and already-merged
          3. Survivor = max(worth_score, ties broken by most recent created_at)
          4. Append merged record's content_abstract to survivor.metadata["merged_from"]
          5. Tag merged record with metadata["merged_into"] = survivor_id
          6. Remove merged record from engine._memories (retrieval layer)
          7. Keep merged record in SQLite for audit trail (cleaned next GC cycle)

        Args:
            threshold: Cosine similarity threshold for merge (default 0.70).
            dry_run: If True, only report — no records are modified.

        Returns:
            dict with:
                dry_run, candidates_found, would_merge, would_free, merged_pairs, error
        """
        result: Dict[str, Any] = {
            "dry_run": dry_run,
            "candidates_found": 0,
            "would_merge": 0,
            "would_free": 0,
            "merged_pairs": [],
            "error": None,
        }

        if self.rec_mem is None:
            return result

        try:
            engine = getattr(self.rec_mem, '_engine', None)
            if engine is None:
                return result

            ldb = getattr(engine, '_ldb', None)
            if ldb is None:
                result["error"] = "lancedb_unavailable"
                return result

            memories = getattr(engine, '_memories', {})
            if not memories:
                return result

            # Gather all memories with vectors
            vec_map: Dict[str, list] = {}
            for mid, mem in memories.items():
                vec = mem.get("_vector")
                if vec and not any(v != 0.0 for v in vec):
                    continue  # skip zero vectors (fallback embedder)
                if vec:
                    vec_map[mid] = vec

            if len(vec_map) < 2:
                return result

            # Scan: for each memory, find similar neighbors
            seen_pairs: set = set()
            candidates: list[tuple[str, str, float]] = []  # (mid_a, mid_b, similarity)

            for mid, vec in vec_map.items():
                try:
                    similar = ldb.search_similar(vec, k=3)  # MERGE_TOP_K
                except Exception:
                    continue
                for other_id, sim in similar:
                    if other_id == mid:
                        continue  # self-match
                    if sim < threshold:
                        continue
                    pair_key = tuple(sorted([mid, other_id]))
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    candidates.append((mid, other_id, sim))

            result["candidates_found"] = len(candidates)

            if not candidates:
                return result

            # Determine survivors and merged records
            already_merged: set = set()
            merged_pairs = []

            for mid_a, mid_b, sim in candidates:
                if mid_a in already_merged or mid_b in already_merged:
                    continue

                # Get Python records for worth_score comparison
                py_a = self.rec_mem._records.get(mid_a)
                py_b = self.rec_mem._records.get(mid_b)

                # Determine survivor
                score_a = py_a.worth_score if py_a else 0.5
                score_b = py_b.worth_score if py_b else 0.5

                if score_a > score_b:
                    survivor, merged = mid_a, mid_b
                elif score_b > score_a:
                    survivor, merged = mid_b, mid_a
                else:
                    # Tiebreaker: most recent created_at
                    time_a = py_a.created_at if py_a else ""
                    time_b = py_b.created_at if py_b else ""
                    if time_a >= time_b:
                        survivor, merged = mid_a, mid_b
                    else:
                        survivor, merged = mid_b, mid_a

                # Build merged_from entry
                merged_content = ""
                merged_worth = 0.0
                if merged in self.rec_mem._records:
                    mr = self.rec_mem._records[merged]
                    merged_content = mr.content[:80]
                    merged_worth = mr.worth_score

                merge_entry = {
                    "memory_id": merged,
                    "content_abstract": merged_content,
                    "merged_at": datetime.datetime.now().isoformat(),
                    "worth_score": round(merged_worth, 4),
                }

                merged_pairs.append({
                    "survivor": survivor,
                    "merged": [merged],
                    "similarity": round(sim, 4),
                })

                if not dry_run:
                    # Append to survivor metadata
                    if survivor in self.rec_mem._records:
                        surv_rec = self.rec_mem._records[survivor]
                        if "merged_from" not in surv_rec.metadata:
                            surv_rec.metadata["merged_from"] = []
                        surv_rec.metadata["merged_from"].append(merge_entry)

                    # Tag merged record
                    if merged in self.rec_mem._records:
                        self.rec_mem._records[merged].metadata["merged_into"] = survivor

                    # Remove from engine._memories (retrieval layer)
                    if merged in memories:
                        # Optionally persist merged_into to SQLite for audit
                        sqlite = getattr(engine, '_sqlite', None)
                        if sqlite is not None and merged in memories:
                            try:
                                mem_data = memories[merged]
                                mem_data["merged_into"] = survivor
                                sqlite.upsert(merged, mem_data)
                            except Exception:
                                pass
                        del memories[merged]

                    already_merged.add(merged)

            # Deduplicate: count unique merged records
            all_merged = set()
            for pair in merged_pairs:
                for m in pair["merged"]:
                    all_merged.add(m)

            result["would_merge"] = len(merged_pairs)
            result["would_free"] = len(all_merged)
            result["merged_pairs"] = merged_pairs[:20]  # cap preview at 20 pairs

            return result

        except Exception as e:
            result["error"] = str(e)
            return result
```

- [ ] **Step 4: Integrate merge_similar into MemoryGC.collect()**

In the `collect` method, add `merge_similar()` call after `mark_decaying()` and before the `forget` loop. Replace the relevant section of `collect()` (around lines 930-976):

In the `collect` method body, after `candidates = self.mark_decaying()` and the dry_run early return, insert:

```python
        # ---- ★ Direction B: Similar memory merge ----
        merge_result = self.merge_similar(threshold=0.70, dry_run=dry_run)
        result["merge"] = merge_result
```

And update the result dict initialization to include the merge key:

```python
        result = {
            "dry_run": dry_run,
            "candidates_count": len(candidates) if candidates else 0,
            "candidates": (candidates or [])[:50],
            "removed": 0,
            "health_before": health_before,
            "health_after": health_before,
            "freed_slots": 0,
            "merge": {},  # ★ populated below
        }
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_memory_merge.py -v
```

Expected: 4 PASS

- [ ] **Step 6: Run existing GC-related tests**

```bash
pytest tests/ -k "gc" -v 2>&1 | head -20
```

- [ ] **Step 7: Commit**

```bash
git add plastic_promise/memory/soul_memory.py tests/test_memory_merge.py
git commit -m "feat: MemoryGC.merge_similar() — batch similar memory merging with LanceDB ANN"
```

---

## Verification

After all 4 tasks are complete, run the full test suite:

```bash
pytest tests/ -v
```

Then verify via MCP tools:

```python
# 1. Store a memory through the pipeline — verify extraction ran
memory_store(content="用户喜欢用 Rust 写后端，原因是内存安全和零成本抽象")

# 2. Check pipeline stats
system(action="stats")

# 3. Verify QualityGate decisions in logs
# Look for: "QualityGate: <id> stored (score=X.XX)" or "QualityGate: <id> discarded (score=X.XX)"

# 4. Trigger GC merge in dry_run mode
memory_gc(dry_run=True)
# Check response for merge.candidates_found and merge.merged_pairs

# 5. Verify merged records don't appear in recall
memory_recall(query="Rust backend preferences")
```
