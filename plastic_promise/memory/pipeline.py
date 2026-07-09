"""MemoryPipeline — 所有记忆的必经处理流水线。

raw → tagged(关键词) → classified(大类L1/L3) → embedded(细分向量) → migrate(主池)
同步处理，从不积压。不是缓存区——是标准流程。
"""

import datetime
import hashlib
import json
import logging
import os
import re
import uuid
from typing import Any

from plastic_promise.core.constants import DEDUP_SIMILARITY_THRESHOLD


class MemoryPipeline:
    """记忆处理流水线 — 所有记忆的必经之路。

    Stages (先大类再细分):
        raw        — 刚存入，临时标签
        tagged     — 关键词提取，噪音过滤
        classified — L1/L3 大类判定
        embedded   — 向量嵌入（细分），准备入主池
    """

    def __init__(
        self, rec_mem=None, embedder=None, tier_manager=None, domain_manager=None, lancedb=None
    ) -> None:
        self._buffer: dict[str, dict[str, Any]] = {}
        self.rec_mem = rec_mem
        self.embedder = embedder
        self._tier_manager = tier_manager
        self._dm = domain_manager
        self._lancedb = lancedb  # vector dedup (None → graceful skip)
        self._last_process: str | None = None
        self._batch_size = 10

    # ================================================================
    # Public API
    # ================================================================

    def store_urgent(
        self,
        content: str,
        memory_type: str = "experience",
        source: str = "user",
        entity_ids: list[str] = None,
        custom_tags: list[str] = None,
        domain_hint: str = None,
        max_llm_calls: int = 3,
        skip_embed: bool = False,
        project_id: str = "project:legacy-global",
        visibility: str = "project",
        source_class: str = "experience",
        created_by_call_id: str = "",
        origin_kind: str = "",
        origin_uri: str = "",
        origin_ref: str = "",
        origin_hash: str = "",
        parent_memory_ids: list[str] | None = None,
        metadata_json: dict | None = None,
    ) -> str | None:
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

            extracted_list = extract_memories(content, max_llm_calls=max_llm_calls)
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
                "skip_embed": skip_embed,
                "project_id": project_id,
                "visibility": visibility,
                "source_class": source_class,
                "created_by_call_id": created_by_call_id,
                "origin_kind": origin_kind,
                "origin_uri": origin_uri,
                "origin_ref": origin_ref,
                "origin_hash": origin_hash,
                "parent_memory_ids": parent_memory_ids or [],
                "metadata_json": dict(metadata_json or {}),
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

            index_fields = self._build_memory_index_fields(
                raw_content=content,
                extracted=record.get("extracted", {}),
                embedder=self.embedder,
            )
            record.update(index_fields)
            metadata = dict(record.get("metadata_json", {}))
            metadata_extracted = dict(metadata.get("extracted", {}))
            metadata_extracted.update(
                {
                    "category": record.get("extracted", {}).get("category", "other"),
                    "l0_abstract": record["l0_abstract"],
                    "l1_summary": record["l1_summary"],
                    "l2_content": record["l2_content"],
                    "confidence": record.get("extracted", {}).get("confidence", 0.5),
                    "importance": record.get("extracted", {}).get("importance", 0.7),
                }
            )
            metadata["extracted"] = metadata_extracted
            metadata["memory_index"] = {
                "embedding_hash": record["embedding_hash"],
                "summary_index_enabled": self._summary_index_enabled(),
            }
            record["metadata_json"] = metadata

            self._buffer[mid] = record
            if first_mid is None:
                first_mid = mid

        # Fix #1: Log multi-extraction for traceability
        if len(extracted_list) > 1:
            logging.info(
                "store_urgent: %d memories extracted from content, returning first ID %s",
                len(extracted_list),
                first_mid,
            )

        return first_mid

    def process_pipeline(self) -> dict[str, Any]:
        """Run the full 4-stage pipeline on all buffered items.

        Returns:
            dict with counts per stage and migration total.
        """
        counts = {
            "raw→tagged": 0,
            "tagged→classified": 0,
            "classified→embedded": 0,
            "embedded→migrated": 0,
        }

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

    def stats(self) -> dict[str, Any]:
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

    @staticmethod
    def _has_nonzero_vector(vec: Any) -> bool:
        return bool(vec) and any(v != 0.0 for v in vec)

    @staticmethod
    def _summary_index_enabled() -> bool:
        value = os.environ.get("PP_MEMORY_SUMMARY_INDEX", "")
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _safe_summary_text(text: Any, max_chars: int = 180) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        return normalized[:max_chars].strip()

    @staticmethod
    def _embedding_model_name(embedder: Any) -> str:
        return str(
            getattr(embedder, "model_name", None)
            or getattr(embedder, "model", None)
            or embedder.__class__.__name__
            if embedder is not None
            else "unknown"
        )

    @classmethod
    def _build_memory_index_fields(
        cls,
        raw_content: str,
        extracted: dict[str, Any] | None = None,
        embedder: Any = None,
    ) -> dict[str, str]:
        extracted = extracted if isinstance(extracted, dict) else {}
        l2_content = str(extracted.get("l2_content") or raw_content or "")
        l0_abstract = cls._safe_summary_text(extracted.get("l0_abstract") or l2_content)
        if not l0_abstract:
            l0_abstract = cls._safe_summary_text(raw_content)
        l1_summary = str(extracted.get("l1_summary") or "").strip()
        if not l1_summary and l0_abstract:
            l1_summary = f"- {l0_abstract}"

        parts = [
            f"L0: {l0_abstract}" if l0_abstract else "",
            f"L1: {l1_summary}" if l1_summary else "",
        ]
        embedding_text = "\n".join(part for part in parts if part).strip()
        model_name = cls._embedding_model_name(embedder)
        embedding_hash = hashlib.sha256(
            f"{model_name}\n{embedding_text}".encode("utf-8")
        ).hexdigest()

        return {
            "raw_content": raw_content or "",
            "l0_abstract": l0_abstract,
            "l1_summary": l1_summary,
            "l2_content": l2_content,
            "embedding_text": embedding_text,
            "embedding_hash": embedding_hash,
            "search_text": l0_abstract or cls._safe_summary_text(l1_summary or l2_content),
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
        # Guard: structured markers carry their own semantics via custom_tags;
        # word-splitting them produces garbage like "[SKILL", "START]".
        if content.startswith(
            (
                "[SKILL START]",
                "[SKILL COMPLETE]",
                "[SKILL ARTIFACT]",
                "[SKILL ABANDONED]",
                "[AUTO INJECT]",
            )
        ):
            return []

        tags: list[str] = []
        seen: set[str] = set()

        # Layer 1: 规则提取 (always)
        has_cjk = bool(re.search(r"[一-鿿]", content))
        if has_cjk:
            for i in range(len(content) - 1):
                bigram = content[i : i + 2]
                if re.search(r"[一-鿿]", bigram) and bigram not in seen:
                    tags.append(bigram)
                    seen.add(bigram)
                if len(tags) >= 5:
                    break
        if not tags:
            tags = [
                w
                for w in re.split(r"\s+|[,，。.!！?？;；:：\n]+", content)
                if len(w) >= 2
                and w.lower()
                not in {
                    "the",
                    "this",
                    "that",
                    "and",
                    "for",
                    "was",
                    "are",
                    "not",
                    "but",
                    "all",
                    "can",
                    "has",
                    "had",
                    "get",
                    "got",
                    "put",
                    "set",
                    "use",
                    "used",
                }
            ][:5]

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
            if hasattr(self, "_dm") and self._dm is not None:
                record["domain"] = self._dm.assign(tags, agent_id=getattr(self, "_owner", ""))
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
            batch = items[i : i + self._batch_size]
            embed_texts = [
                r.get("embedding_text") or r["content"]
                if self._summary_index_enabled()
                else r["content"]
                for _, r in batch
            ]
            # Skip Ollama embed for items marked skip_embed (e.g. auto_inject structured content)
            skip_set = {mid for mid, r in batch if r.get("skip_embed")}
            try:
                if skip_set:
                    vectors = []
                    for (mid, r), embed_text in zip(batch, embed_texts):
                        if mid in skip_set:
                            vectors.append([0.0] * self.embedder.dim)
                        else:
                            vectors.append(self.embedder.embed(embed_text))
                else:
                    vectors = self.embedder.embed_batch(embed_texts)
            except Exception as e:
                logging.warning(
                    "Embed batch failed, deferring %d items (skip_set=%d): %s",
                    len(batch),
                    len(skip_set),
                    e,
                )
                for _mid, record in batch:
                    tags = record.setdefault("tags", [])
                    if "embed:deferred" not in tags:
                        tags.append("embed:deferred")
                continue  # stay in classified stage, retry next cycle
            for (mid, record), vec in zip(batch, vectors):
                # Classified-stage guard: if rec_mem is None and vector is zero,
                # defer to avoid permanent zero-vector storage
                if self.rec_mem is None and vec and not any(v != 0.0 for v in vec):
                    logging.warning(
                        "Zero vector + no rec_mem for %s, deferring in classified",
                        mid,
                    )
                    record.setdefault("tags", [])
                    if "embed:deferred" not in record["tags"]:
                        record["tags"].append("embed:deferred")
                    continue  # stay in classified
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

                engine = getattr(self.rec_mem, "_engine", None)
                vec = record.get("vector")
                has_nonzero_vector = self._has_nonzero_vector(vec)

                # ---- Zero-vector guard: reject fallback embeddings ----
                if vec and not has_nonzero_vector and not record.get("skip_embed"):
                    logging.warning(
                        "Zero vector detected for %s, deferring back to classified",
                        mid,
                    )
                    record.setdefault("tags", [])
                    if "embed:fallback" not in record["tags"]:
                        record["tags"].append("embed:fallback")
                    record["stage"] = "classified"  # rollback for retry
                    continue

                # ---- Step 4a: Vector dedup ----
                if has_nonzero_vector and self._lancedb is not None:
                    try:
                        dup_id = self._lancedb.check_duplicate(
                            vec, threshold=DEDUP_SIMILARITY_THRESHOLD
                        )
                        if dup_id and engine is not None:
                            now_iso = datetime.datetime.now().isoformat()
                            # Increment access_count + worth_success on existing record
                            # Fix #2: Guard against dup_id missing from engine._memories (SQLite-only memory)
                            if engine.memory_exists(dup_id):
                                engine.increment_field(dup_id, "access_count", 1)
                                engine.increment_field(dup_id, "worth_success", 1)
                                engine.update_memory_fields(dup_id, last_accessed=now_iso)
                                mem = engine.get_memory_dict(dup_id)
                                existing_eids = set(mem.get("entity_ids", []) if mem else [])
                                new_eids = set(record.get("entity_ids", []))
                                if new_eids - existing_eids:
                                    engine.update_memory_fields(
                                        dup_id, entity_ids=list(existing_eids | new_eids)
                                    )
                            if dup_id in getattr(self.rec_mem, "_records", {}):
                                py_rec = self.rec_mem._records[dup_id]
                                py_rec.access_count += 1
                                py_rec.worth_success += 1
                                py_rec.last_accessed = now_iso
                                # Merge entity_ids into Python record as well
                                py_eids = set(getattr(py_rec, "entity_ids", []))
                                new_eids = set(record.get("entity_ids", []))
                                if new_eids - py_eids:
                                    py_rec.entity_ids = list(py_eids | new_eids)
                                # ---- Gap 1 fix: Recompute effective_half_life via AccessReinforcement ----
                                try:
                                    from plastic_promise.core.constants import DECAY_CONFIG
                                    from plastic_promise.core.decay_engine import (
                                        AccessReinforcement,
                                    )

                                    tier = getattr(py_rec, "tier", "L1")
                                    base_hl = DECAY_CONFIG.get(tier, DECAY_CONFIG["default"])[
                                        "half_life_days"
                                    ]
                                    reinforcer = AccessReinforcement()
                                    _, new_hl = reinforcer.compute_boost(
                                        access_count=py_rec.access_count,
                                        last_accessed=now_iso,
                                        base_half_life=base_hl,
                                        is_auto_recall=False,
                                        current_time_str=now_iso,
                                    )
                                    py_rec.effective_half_life = new_hl
                                    if engine.memory_exists(dup_id):
                                        engine.update_memory_fields(
                                            dup_id, effective_half_life=new_hl
                                        )
                                except Exception:
                                    pass  # Graceful: boost is a quality improvement, not a hard gate
                            # SQLite incremental update — includes last_accessed (Fix #6) and entity_ids merge (Fix #7)
                            sqlite = getattr(engine, "_sqlite", None)
                            if sqlite is not None:
                                try:
                                    # Merge entity_ids
                                    existing_row = sqlite._conn.execute(
                                        "SELECT entity_ids FROM memories WHERE id = ?", (dup_id,)
                                    ).fetchone()
                                    merged_eids = list(set(record.get("entity_ids", [])))
                                    if existing_row and existing_row[0]:
                                        try:
                                            old_eids = (
                                                json.loads(existing_row[0])
                                                if isinstance(existing_row[0], str)
                                                else existing_row[0]
                                            )
                                            merged_eids = list(set(old_eids + merged_eids))
                                        except Exception:
                                            pass
                                    sqlite._conn.execute(
                                        "UPDATE memories SET access_count = access_count + 1, "
                                        "worth_success = worth_success + 1, "
                                        "last_accessed = ?, entity_ids = ? WHERE id = ?",
                                        (now_iso, json.dumps(merged_eids), dup_id),
                                    )
                                    sqlite._conn.commit()
                                except Exception:
                                    pass
                            del self._buffer[mid]
                            logging.info(
                                "Dedup: %s -> merged into %s (similarity >= 0.85)", mid, dup_id
                            )
                            continue  # skip store
                    except Exception as e:
                        logging.warning(
                            "Dedup check failed for %s: %s -- proceeding with store", mid, e
                        )

                # ---- Step 4b: QualityGate scoring ----
                extracted = record.get("extracted", {})
                # Defensive: handle both dict and string-serialized extracted field
                if isinstance(extracted, str):
                    try:
                        extracted = json.loads(extracted) if extracted.strip() else {}
                    except (json.JSONDecodeError, TypeError):
                        extracted = {}
                elif not isinstance(extracted, dict):
                    extracted = {}
                tags = record.get("tags", [])
                domain_hint = record.get("domain", "uncategorized")
                created_at = record.get("created_at")

                tier = record.get("tier", "L1")
                gate_score = gate.score(
                    extracted=extracted,
                    tags=tags,
                    domain_hint=domain_hint,
                    created_at=created_at,
                    tier=tier,
                )
                decision = QualityGate.decide(gate_score)

                if decision == "discard":
                    del self._buffer[mid]
                    logging.info(
                        "QualityGate: %s discarded (score=%.3f < %.2f)",
                        mid,
                        gate_score,
                        QualityGate.THRESHOLD_LOW,
                    )
                    continue

                # ---- Step 4c: Store ----
                # Extract category from smart_extractor result (preference/fact/decision/entity/event/pattern)
                extracted = record.get("extracted", {})
                # Defensive: same string/dict guard as Step 4b
                if isinstance(extracted, str):
                    try:
                        extracted = json.loads(extracted) if extracted.strip() else {}
                    except (json.JSONDecodeError, TypeError):
                        extracted = {}
                elif not isinstance(extracted, dict):
                    extracted = {}
                extracted_category = extracted.get("category", "other")
                extracted_confidence = extracted.get("confidence", 0.5)

                # Tag for background LLM refinement when rule classification is uncertain
                if extracted_category == "other" or extracted_confidence < 0.5:
                    if "llm_pending:true" not in tags:
                        tags.append("llm_pending:true")
                metadata_json = dict(record.get("metadata_json", {}))
                metadata_json.update(
                    {
                        "raw_content": record.get("raw_content", ""),
                        "l0_abstract": record.get("l0_abstract", ""),
                        "l1_summary": record.get("l1_summary", ""),
                        "l2_content": record.get("l2_content", ""),
                        "embedding_text": record.get("embedding_text", ""),
                        "embedding_hash": record.get("embedding_hash", ""),
                    }
                )
                stored = self.rec_mem.store(
                    content=record["content"],
                    memory_type=record["memory_type"],
                    source=record["source"],
                    entity_ids=record.get("entity_ids", []),
                    tags=tags,
                    domain=domain_hint,
                    category=extracted_category,
                    memory_id=record.get("memory_id"),
                    project_id=record.get("project_id", "project:legacy-global"),
                    visibility=record.get("visibility", "project"),
                    source_class=record.get("source_class", "experience"),
                    created_by_call_id=record.get("created_by_call_id", ""),
                    origin_kind=record.get("origin_kind", ""),
                    origin_uri=record.get("origin_uri", ""),
                    origin_ref=record.get("origin_ref", ""),
                    origin_hash=record.get("origin_hash", ""),
                    parent_memory_ids=record.get("parent_memory_ids", []),
                    metadata_json=metadata_json,
                )

                # Attach quality metadata
                if decision == "low_quality":
                    if (
                        hasattr(self.rec_mem, "_records")
                        and stored.memory_id in self.rec_mem._records
                    ):
                        py_rec = self.rec_mem._records[stored.memory_id]
                        py_rec.metadata["quality"] = "low_quality"
                        py_rec.metadata["gate_score"] = round(gate_score, 4)
                    logging.info(
                        "QualityGate: %s stored with low_quality tag (score=%.3f)",
                        stored.memory_id,
                        gate_score,
                    )

                # ---- Gap 3: Initialize decay_multiplier + effective_half_life on store ----
                try:
                    from plastic_promise.core.constants import DECAY_CONFIG
                    from plastic_promise.core.decay_engine import WeibullDecayCalculator

                    wdc = WeibullDecayCalculator()
                    dm = wdc.compute_decay(tier, created_at or datetime.datetime.now().isoformat())
                    tier_cfg = DECAY_CONFIG.get(tier, DECAY_CONFIG["default"])
                    base_hl = tier_cfg["half_life_days"]
                    # New memory: effective_half_life starts at base (no access history yet)
                    if (
                        hasattr(self.rec_mem, "_records")
                        and stored.memory_id in self.rec_mem._records
                    ):
                        py_rec = self.rec_mem._records[stored.memory_id]
                        py_rec.decay_multiplier = dm
                        py_rec.effective_half_life = base_hl
                    if engine is not None:
                        engine.update_memory_fields(
                            stored.memory_id, decay_multiplier=dm, effective_half_life=base_hl
                        )
                    sqlite = getattr(engine, "_sqlite", None)
                    if sqlite is not None:
                        sqlite._conn.execute(
                            "UPDATE memories SET decay_multiplier = ?, effective_half_life = ? WHERE id = ?",
                            (dm, base_hl, stored.memory_id),
                        )
                        sqlite._conn.commit()
                except Exception:
                    pass  # Graceful: decay init is a quality improvement

                # ---- Existing: vector + tags + domain persistence ----
                if engine is not None:
                    if has_nonzero_vector:
                        engine.update_memory_fields(stored.memory_id, _vector=vec)
                        ldb = getattr(engine, "_ldb", None)
                        if ldb is not None:
                            try:
                                lancedb_text = (
                                    record.get("search_text", "")
                                    if self._summary_index_enabled()
                                    else record.get("content", "")
                                )
                                ldb.insert(
                                    memory_id=stored.memory_id,
                                    vector=vec,
                                    text=lancedb_text,
                                    tier=record.get("tier", "L1"),
                                    category=record.get("category", "other"),
                                    scope=record.get("scope", "global"),
                                )
                            except Exception as e:
                                logging.warning(
                                    "LanceDB dual-write failed for %s: %s", stored.memory_id, e
                                )
                    engine.update_memory_fields(
                        stored.memory_id,
                        tags=tags,
                        domain=domain_hint,
                        project_id=record.get("project_id", "project:legacy-global"),
                        visibility=record.get("visibility", "project"),
                        source_class=record.get("source_class", "experience"),
                        created_by_call_id=record.get("created_by_call_id", ""),
                        origin_kind=record.get("origin_kind", ""),
                        origin_uri=record.get("origin_uri", ""),
                        origin_ref=record.get("origin_ref", ""),
                        origin_hash=record.get("origin_hash", ""),
                        parent_memory_ids=record.get("parent_memory_ids", []),
                        metadata_json=metadata_json,
                    )
                    sqlite = getattr(engine, "_sqlite", None)
                    if sqlite is not None:
                        import json

                        sqlite._conn.execute(
                            "UPDATE memories SET tags = ?, domain = ?, project_id = ?, "
                            "visibility = ?, source_class = ?, created_by_call_id = ?, "
                            "origin_kind = ?, origin_uri = ?, origin_ref = ?, origin_hash = ?, "
                            "parent_memory_ids = ?, metadata_json = ? WHERE id = ?",
                            (
                                json.dumps(tags),
                                domain_hint,
                                record.get("project_id", "project:legacy-global"),
                                record.get("visibility", "project"),
                                record.get("source_class", "experience"),
                                record.get("created_by_call_id", ""),
                                record.get("origin_kind", ""),
                                record.get("origin_uri", ""),
                                record.get("origin_ref", ""),
                                record.get("origin_hash", ""),
                                json.dumps(record.get("parent_memory_ids", []), ensure_ascii=False),
                                json.dumps(metadata_json, ensure_ascii=False),
                                stored.memory_id,
                            ),
                        )
                        sqlite._conn.commit()

                # Rebuild entity edges
                entity_ids = record.get("entity_ids", [])
                if entity_ids and engine is not None:
                    for eid in entity_ids:
                        edge = {
                            "from": stored.memory_id,
                            "to": eid,
                            "relation": "references",
                            "weight": 0.5,
                        }
                        graph_edges = getattr(engine, "_graph_edges", [])
                        if edge not in graph_edges:
                            graph_edges.append(edge)

                del self._buffer[mid]
                count += 1
            except Exception as e:
                logging.warning("Migrate failed for %s: %s", mid, e)

        return count
