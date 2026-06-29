"""MemoryPipeline — 所有记忆的必经处理流水线。

raw → tagged(关键词) → classified(大类L1/L3) → embedded(细分向量) → migrate(主池)
同步处理，从不积压。不是缓存区——是标准流程。
"""

import uuid
import datetime
import re
from typing import Any, Dict, List, Optional


class MemoryPipeline:
    """记忆处理流水线 — 所有记忆的必经之路。

    Stages (先大类再细分):
        raw        — 刚存入，临时标签
        tagged     — 关键词提取，噪音过滤
        classified — L1/L3 大类判定
        embedded   — 向量嵌入（细分），准备入主池
    """

    def __init__(self, rec_mem=None, embedder=None, tier_manager=None, domain_manager=None) -> None:
        self._buffer: Dict[str, Dict[str, Any]] = {}
        self.rec_mem = rec_mem
        self.embedder = embedder
        self._tier_manager = tier_manager
        self._dm = domain_manager
        self._last_process: Optional[str] = None
        self._batch_size = 10

    # ================================================================
    # Public API
    # ================================================================

    def store_urgent(
        self, content: str, memory_type: str = "experience", source: str = "user",
        entity_ids: list[str] = None,
    ) -> str:
        """Store a memory urgently with temporary tags, skipping embedding.

        Args:
            content: Memory text content.
            memory_type: Memory type (experience/reflection/principle/feedback).
            source: Source identifier (user/agent/system).
            entity_ids: Auto-extracted entity references (for graph linking).

        Returns:
            memory_id with 'fuzzy_' prefix.
        """
        mid = f"fuzzy_{uuid.uuid4().hex[:12]}"
        tags = self._extract_semantic_tags(content)
        self._buffer[mid] = {
            "memory_id": mid,
            "content": content,
            "memory_type": memory_type,
            "source": source,
            "entity_ids": entity_ids or [],
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

    def _extract_semantic_tags(self, content: str, use_llm: bool = True) -> list[str]:
        """提取语义标签。

        两层策略:
          1. 规则层 (免费): CJK bigram + 关键词正则 + 种子标签匹配
          2. 语义层 (可选): Ollama LLM 提取 3-5 个语义标签
        合并去重，上限 10 个。
        """
        tags: list[str] = []
        seen: set[str] = set()

        # Layer 1: 规则提取 (always)
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
            tags = [w for w in re.split(r'\s+|[,，。.!！?？;；:：\n]+', content)
                    if len(w) >= 2 and w.lower() not in {'the','this','that','and','for','was','are','not','but','all','can','has','had','get','got','put','set','use','used'}][:5]

        # Layer 2: 种子标签匹配 (从预定义域标签中匹配)
        try:
            from plastic_promise.core.domain_manager import PREDEFINED_DOMAINS
            for domain_cfg in PREDEFINED_DOMAINS.values():
                for seed_tag in domain_cfg.get("tags", set()):
                    if seed_tag.lower() in content.lower() and seed_tag not in seen:
                        tags.append(seed_tag)
                        seen.add(seed_tag)
        except Exception:
            pass

        return tags[:10]

    # ================================================================
    # Pipeline Stages
    # ================================================================

    def _process_raw_to_tagged(self) -> int:
        """Stage 1: Confirm tags for raw items, filter noise, move to tagged."""
        count = 0
        items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "raw"]
        for mid, record in items:
            try:
                from plastic_promise.core.noise_filter import is_noise
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
        """Stage 2: 大类分 — classify tier (L1/L3) for tagged items, then domain assignment."""
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

            # 新增: domain 分配
            tags = record.get("tags", [])
            if hasattr(self, '_dm') and self._dm is not None:
                record["domain"] = self._dm.assign(tags, agent_id=getattr(self, '_owner', ''))
            else:
                record["domain"] = "uncategorized"

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
                        tags=record.get("tags", []),
                        domain=record.get("domain", "uncategorized"),
                    )
                    # Save vector for _vector_retrieval (细匹配)
                    vec = record.get("vector")
                    if vec and hasattr(self.rec_mem, '_engine'):
                        engine = self.rec_mem._engine
                        engine._memories[stored.memory_id]["_vector"] = vec
                        engine._memories[stored.memory_id]["tags"] = record.get("tags", [])
                        engine._memories[stored.memory_id]["domain"] = record.get("domain", "uncategorized")
                    # Rebuild entity edges from fuzzy buffer to main pool (原则 #6)
                    entity_ids = record.get("entity_ids", [])
                    if entity_ids and hasattr(self.rec_mem, '_engine'):
                        engine = self.rec_mem._engine
                        for eid in entity_ids:
                            edge = {"from": stored.memory_id, "to": eid,
                                    "relation": "references", "weight": 0.5}
                            if edge not in engine._graph_edges:
                                engine._graph_edges.append(edge)
                del self._buffer[mid]
                count += 1
            except Exception:
                pass
        return count
