"""Fuzzy Buffer — deferred memory embedding pipeline.

When the embedding service is unavailable, memories are stored urgently
with temporary tags in a buffer. The buffer is processed in the background
through a 4-stage pipeline: raw → tagged → embedded → classified → migrate.
"""

import uuid
import datetime
import re
from typing import Any, Dict, List, Optional


class FuzzyBuffer:
    """Deferred memory processing buffer with 4-stage pipeline.

    Stages (先大类再细分):
        raw        — just arrived, basic tags only
        tagged     — keywords extracted, noise filtered
        classified — tier (L1/L3) determined (大类)
        embedded   — vectors generated (细分), ready to migrate
    """

    def __init__(self, rec_mem=None, embedder=None, tier_manager=None) -> None:
        self._buffer: Dict[str, Dict[str, Any]] = {}
        self.rec_mem = rec_mem
        self.embedder = embedder
        self._tier_manager = tier_manager
        self._last_process: Optional[str] = None
        self._batch_size = 10

    # ================================================================
    # Public API
    # ================================================================

    def store_urgent(
        self, content: str, memory_type: str = "experience", source: str = "user"
    ) -> str:
        """Store a memory urgently with temporary tags, skipping embedding.

        Args:
            content: Memory text content.
            memory_type: Memory type (experience/reflection/principle/feedback).
            source: Source identifier (user/agent/system).

        Returns:
            memory_id with 'fuzzy_' prefix.
        """
        mid = f"fuzzy_{uuid.uuid4().hex[:12]}"
        tags = self._extract_tags(content)
        self._buffer[mid] = {
            "memory_id": mid,
            "content": content,
            "memory_type": memory_type,
            "source": source,
            "stage": "raw",
            "tags": tags,
            "vector": None,
            "tier": None,
            "created_at": datetime.datetime.now().isoformat(),
            "processed_at": None,
        }
        return mid

    def process_pipeline(self) -> Dict[str, Any]:
        """Run the full 4-stage pipeline on all buffered items.

        Returns:
            dict with counts per stage and migration total.
        """
        counts = {"raw→tagged": 0, "tagged→classified": 0,
                  "classified→embedded": 0, "embedded→migrated": 0}

        # Stage 1: raw → tagged (noise filter + keyword extraction)
        counts["raw→tagged"] = self._process_raw_to_tagged()

        # Stage 2: tagged → classified (大类: tier L1/L3 判定)
        counts["tagged→classified"] = self._process_tagged_to_classified()

        # Stage 3: classified → embedded (细分: batch embed 向量化)
        counts["classified→embedded"] = self._process_classified_to_embedded()

        # Stage 4: embedded → migrate to main pool
        counts["embedded→migrated"] = self._process_embedded_to_migrate()

        self._last_process = datetime.datetime.now().isoformat()
        return {
            "pipeline": counts,
            "total_processed": sum(counts.values()),
            "buffer_remaining": len(self._buffer),
            "timestamp": self._last_process,
        }

    def stats(self) -> Dict[str, Any]:
        """Return buffer statistics.

        Returns:
            dict with total, by_stage counts, oldest_pending, last_process.
        """
        by_stage = {"raw": 0, "tagged": 0, "embedded": 0, "classified": 0}
        for r in self._buffer.values():
            stage = r.get("stage", "raw")
            if stage in by_stage:
                by_stage[stage] += 1
        total = len(self._buffer)
        oldest = None
        if self._buffer:
            oldest = min(r.get("created_at", "") for r in self._buffer.values())
        return {
            "total": total,
            "by_stage": by_stage,
            "oldest_pending": oldest,
            "last_process": self._last_process,
        }

    # ================================================================
    # Internal: Tag Extraction
    # ================================================================

    def _extract_tags(self, content: str) -> List[str]:
        """Extract up to 5 CJK bigram keywords as temporary tags.

        For non-CJK content, falls back to whitespace-split words (≥2 chars).
        """
        tags: List[str] = []
        seen: set = set()
        has_cjk = bool(re.search(r'[一-鿿]', content))
        if has_cjk:
            for i in range(len(content) - 1):
                bigram = content[i:i+2]
                if re.search(r'[一-鿿]', bigram) and bigram not in seen:
                    tags.append(bigram)
                    seen.add(bigram)
                if len(tags) >= 5:
                    break
        if not tags:
            tags = [w for w in content.split() if len(w) >= 2][:5]
        return tags

    # ================================================================
    # Pipeline Stages
    # ================================================================

    def _process_raw_to_tagged(self) -> int:
        """Stage 1: Confirm tags for raw items, filter noise, move to tagged."""
        count = 0
        items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "raw"]
        for mid, record in items:
            try:
                from plastic_promise.noise_filter import is_noise
                if is_noise(record["content"]):
                    del self._buffer[mid]
                    continue
            except Exception:
                pass
            record["stage"] = "tagged"
            record["processed_at"] = datetime.datetime.now().isoformat()
            count += 1
        return count

    def _process_tagged_to_classified(self) -> int:
        """Stage 2: 大类分 — classify tier (L1/L3) for tagged items using MemoryTierManager."""
        items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "tagged"]
        count = 0
        for mid, record in items:
            if self._tier_manager is not None:
                try:
                    from plastic_promise.memory.soul_memory import MemoryRecord
                    mr = MemoryRecord(
                        content=record["content"],
                        memory_type=record["memory_type"],
                        source=record["source"],
                    )
                    record["tier"] = self._tier_manager.classify_tier(mr)
                except Exception:
                    record["tier"] = "L1"
            else:
                record["tier"] = "L1"
            record["stage"] = "classified"
            record["processed_at"] = datetime.datetime.now().isoformat()
            count += 1
        return count

    def _process_classified_to_embedded(self) -> int:
        """Stage 3: 细分 — batch-embed classified items, move to embedded."""
        items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "classified"]
        if not items or self.embedder is None:
            return 0

        count = 0
        for i in range(0, len(items), self._batch_size):
            batch = items[i:i + self._batch_size]
            contents = [r["content"] for _, r in batch]
            try:
                vectors = self.embedder.embed_batch(contents)
            except Exception:
                vectors = [[0.0] * self.embedder.dim for _ in batch]
            for (mid, record), vec in zip(batch, vectors):
                record["vector"] = vec
                record["stage"] = "embedded"
                record["processed_at"] = datetime.datetime.now().isoformat()
                count += 1
        return count

    def _process_embedded_to_migrate(self) -> int:
        """Stage 4: Migrate embedded items to main memory pool via RecMem."""
        items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "embedded"]
        count = 0
        for mid, record in items:
            try:
                if self.rec_mem is not None:
                    stored = self.rec_mem.store(
                        content=record["content"],
                        memory_type=record["memory_type"],
                        source=record["source"],
                    )
                    # Save vector for _vector_retrieval (细匹配)
                    vec = record.get("vector")
                    if vec and hasattr(self.rec_mem, '_engine'):
                        engine = self.rec_mem._engine
                        engine._memories[stored.memory_id]["_vector"] = vec
                del self._buffer[mid]
                count += 1
            except Exception:
                pass
        return count
