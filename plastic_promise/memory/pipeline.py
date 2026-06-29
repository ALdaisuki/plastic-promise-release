"""MemoryPipeline — 所有记忆的必经处理流水线。

raw → tagged(关键词) → classified(大类L1/L3) → embedded(细分向量) → migrate(主池)
同步处理，从不积压。不是缓存区——是标准流程。
"""

import uuid
import datetime
import logging
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

    def __init__(self, rec_mem=None, embedder=None, tier_manager=None,
                 domain_manager=None, lancedb=None) -> None:
        self._buffer: Dict[str, Dict[str, Any]] = {}
        self.rec_mem = rec_mem
        self.embedder = embedder
        self._tier_manager = tier_manager
        self._dm = domain_manager
        self._lancedb = lancedb  # vector dedup (None → graceful skip)
        self._last_process: Optional[str] = None
        self._batch_size = 10

    # ================================================================
    # Public API
    # ================================================================

    def store_urgent(
        self, content: str, memory_type: str = "experience", source: str = "user",
        entity_ids: list[str] = None, custom_tags: list[str] = None,
        domain_hint: str = None,
    ) -> Optional[str]:
        """Store a memory with smart extraction, then through pipeline.

        Calls extract_memories() for pre-extraction.
        - 0 results + whitespace content → returns None (pure noise)
        - 0 results + substantive content → raw content fallback
        - 1 result → returns str memory_id
        - N results → all N enter buffer; returns first memory_id as str

        Returns:
            memory_id with 'fuzzy_' prefix, or None if extraction yields nothing.
        """
        # Step 0: Smart extraction
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

            # Tags: semantic extraction OR derived from ExtractedMemory
            if em is not None and em.category:
                # Fix #5: Derive tags from extraction result, skip redundant _extract_semantic_tags
                tags = [f"cat:{em.category}"]
            else:
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

        # Fix #1: Log multi-extraction for traceability
        if len(extracted_list) > 1:
            logging.info("store_urgent: %d memories extracted from content, returning first ID %s",
                         len(extracted_list), first_mid)

        return first_mid

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
        """Stage 4: Dedup → QualityGate → Migrate embedded items to main pool.

        Direction B enhancements:
          1. Vector dedup via LanceDB.check_duplicate() (cos >= 0.85 -> bump counters)
          2. QualityGate composite scoring (4-dim x 0.25)
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

                # ---- Step 4a: Vector dedup ----
                if vec and self._lancedb is not None:
                    try:
                        dup_id = self._lancedb.check_duplicate(
                            vec, threshold=0.85  # DEDUP_SIMILARITY_THRESHOLD
                        )
                        if dup_id and engine is not None:
                            now_iso = datetime.datetime.now().isoformat()
                            # Increment access_count + worth_success on existing record
                            # Fix #2: Guard against dup_id missing from engine._memories (SQLite-only memory)
                            if dup_id in getattr(engine, '_memories', {}):
                                engine._memories[dup_id]["access_count"] = (
                                    engine._memories[dup_id].get("access_count", 0) + 1
                                )
                                engine._memories[dup_id]["worth_success"] = (
                                    engine._memories[dup_id].get("worth_success", 0) + 1
                                )
                                # Fix #6: Update last_accessed so Direction A reinforcement isn't broken
                                engine._memories[dup_id]["last_accessed"] = now_iso
                            if dup_id in getattr(self.rec_mem, '_records', {}):
                                py_rec = self.rec_mem._records[dup_id]
                                py_rec.access_count += 1
                                py_rec.worth_success += 1
                                py_rec.last_accessed = now_iso
                            # SQLite incremental update — includes last_accessed (Fix #6)
                            sqlite = getattr(engine, '_sqlite', None)
                            if sqlite is not None:
                                try:
                                    sqlite._conn.execute(
                                        "UPDATE memories SET access_count = access_count + 1, "
                                        "worth_success = worth_success + 1, "
                                        "last_accessed = ? WHERE id = ?",
                                        (now_iso, dup_id)
                                    )
                                    sqlite._conn.commit()
                                except Exception:
                                    pass
                            del self._buffer[mid]
                            logging.info("Dedup: %s -> merged into %s (similarity >= 0.85)", mid, dup_id)
                            continue  # skip store
                    except Exception as e:
                        logging.warning("Dedup check failed for %s: %s -- proceeding with store", mid, e)

                # ---- Step 4b: QualityGate scoring ----
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
