"""MemoryPipeline — 所有记忆的必经处理流水线。

raw → tagged(关键词) → classified(大类L1/L3) → embedded(细分向量) → migrate(主池)
同步处理，从不积压。不是缓存区——是标准流程。
"""

import datetime
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from plastic_promise.core.constants import DEDUP_SIMILARITY_THRESHOLD
from plastic_promise.core.memory_index import (
    IndexMaterial,
    effective_embedding_model_name,
    index_metadata,
    initial_index_policy,
    metadata_with_index_material,
    prepare_index_material,
    read_persisted_index_material,
    resolve_index_material,
)
from plastic_promise.core.synthesis_retrieval import (
    engine_memory_is_governed_synthesis,
)

_TERMINAL_EMBED_ERRORS = frozenset({"structure_chunking_source_too_large"})


@dataclass(frozen=True)
class PreparedMemory:
    """Immutable, fully prepared memory candidate with no persistence effects."""

    content: str
    category: str
    tier: str
    tags: tuple[str, ...]
    vector: tuple[float, ...]
    index_material: IndexMaterial
    metadata: Mapping[str, Any]


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
        self._rejections: dict[str, dict[str, str]] = {}

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
        if str(memory_type).strip().casefold() == "synthesis":
            raise RuntimeError("synthesis_requires_governed_store")
        from plastic_promise.core.memory_proposals import (
            PROPOSAL_CATEGORIES,
            ProposalPolicyError,
            has_trusted_internal_origin,
            proposal_mode,
        )

        protected_class = str(source_class or "").strip().casefold()
        if (
            proposal_mode() == "on"
            and protected_class in PROPOSAL_CATEGORIES | {"user_fact"}
            and not has_trusted_internal_origin(
                {
                    "source": source,
                    "origin_kind": origin_kind,
                    "origin_uri": origin_uri,
                }
            )
        ):
            raise ProposalPolicyError("approval_required")
        index_policy = initial_index_policy(summary_index_enabled=self._summary_index_enabled())

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
                policy=index_policy,
                domain=record["domain"],
                category=record.get("extracted", {}).get("category", "other"),
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
            metadata["memory_index"] = index_metadata(
                IndexMaterial(
                    vector_text=record["embedding_text"],
                    search_text=record["search_text"],
                    policy=index_policy,
                    embedding_hash=record["embedding_hash"],
                    model_name=self._embedding_model_name(self.embedder),
                )
            )
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

    def prepare_approved_candidate(
        self,
        content: str,
        *,
        category: str,
        source: str = "user",
        source_class: str = "user_fact",
        custom_tags: list[str] | None = None,
        domain_hint: str | None = None,
        project_id: str = "project:legacy-global",
        visibility: str = "project",
        created_by_call_id: str = "",
        origin_kind: str = "memory_proposal",
        origin_uri: str = "",
        origin_ref: str = "",
        origin_hash: str = "",
        metadata_json: Mapping[str, Any] | None = None,
    ) -> PreparedMemory:
        """Prepare an approved proposal without mutating any persistence surface."""
        normalized_content = " ".join(str(content or "").split())
        normalized_category = str(category or "").strip().casefold()
        if not normalized_content:
            raise ValueError("approved_candidate_content_required")
        if normalized_category not in {"fact", "preference", "decision"}:
            raise ValueError("approved_candidate_category_invalid")
        if self.embedder is None:
            raise RuntimeError("approved_candidate_embedder_unavailable")

        from plastic_promise.smart_extractor import extract_memories

        extracted_items = list(extract_memories(normalized_content, max_llm_calls=3) or [])
        if len(extracted_items) != 1:
            raise RuntimeError("approved_candidate_extraction_uncertain")
        extracted_item = extracted_items[0]
        extracted_category = str(getattr(extracted_item, "category", "") or "").casefold()
        if extracted_category and extracted_category != normalized_category:
            raise RuntimeError("approved_candidate_category_mismatch")

        extracted = {
            "category": normalized_category,
            "l0_abstract": str(getattr(extracted_item, "l0_abstract", "") or ""),
            "l1_summary": str(getattr(extracted_item, "l1_summary", "") or ""),
            "l2_content": str(getattr(extracted_item, "l2_content", "") or normalized_content),
            "confidence": float(getattr(extracted_item, "confidence", 0.0) or 0.0),
            "importance": float(getattr(extracted_item, "importance", 0.7) or 0.7),
        }
        tags = [f"cat:{normalized_category}"]
        if custom_tags:
            tags.extend(str(tag) for tag in custom_tags if str(tag).strip())
        tags = list(dict.fromkeys(tags))
        domain = str(domain_hint or "uncategorized")
        tier = "L1"

        index_policy = initial_index_policy(summary_index_enabled=self._summary_index_enabled())
        index_fields = self._build_memory_index_fields(
            raw_content=normalized_content,
            extracted=extracted,
            embedder=self.embedder,
            policy=index_policy,
            domain=domain,
            category=normalized_category,
        )
        material = IndexMaterial(
            vector_text=index_fields["embedding_text"],
            search_text=index_fields["search_text"],
            policy=index_policy,
            embedding_hash=index_fields["embedding_hash"],
            model_name=self._embedding_model_name(self.embedder),
        )
        vector = tuple(float(value) for value in self.embedder.embed(material.vector_text))
        if not self._has_nonzero_vector(vector):
            raise RuntimeError("approved_candidate_embedding_failed")

        from plastic_promise.core.quality_gate import QualityGate

        gate = QualityGate()
        gate_score = gate.score(
            extracted=extracted,
            tags=tags,
            domain_hint=domain,
            created_at=None,
            tier=tier,
        )
        decision = gate.decide(gate_score)
        if decision == "discard":
            raise RuntimeError("approved_candidate_quality_failed")

        metadata = dict(metadata_json or {})
        metadata.update(
            {
                "category": normalized_category,
                "domain": domain,
                "importance": extracted["importance"],
                "raw_content": normalized_content,
                "l0_abstract": index_fields["l0_abstract"],
                "l1_summary": index_fields["l1_summary"],
                "l2_content": index_fields["l2_content"],
                "gate_score": round(gate_score, 4),
                "quality": decision,
                "source": source,
                "source_class": source_class,
                "project_id": project_id,
                "visibility": visibility,
                "created_by_call_id": created_by_call_id,
                "origin_kind": origin_kind,
                "origin_uri": origin_uri,
                "origin_ref": origin_ref,
                "origin_hash": origin_hash,
                "memory_index": index_metadata(material),
            }
        )
        return PreparedMemory(
            content=normalized_content,
            category=normalized_category,
            tier=tier,
            tags=tuple(tags),
            vector=vector,
            index_material=material,
            metadata=MappingProxyType(metadata),
        )

    def prepare_correction(
        self,
        current: Mapping[str, Any],
        new_content: str,
    ) -> PreparedMemory:
        """Build replacement material for one existing ordinary memory.

        This preparation path has no persistence effects.  It deliberately
        reuses the source row's index policy and model identity so a content
        correction does not silently migrate its retrieval contract.
        """
        if not isinstance(current, Mapping):
            raise ValueError("correction_source_invalid")
        normalized_content = " ".join(str(new_content or "").split())
        previous_content = " ".join(str(current.get("content") or "").split())
        if not normalized_content:
            raise ValueError("correction_content_required")
        if normalized_content == previous_content:
            raise ValueError("correction_content_unchanged")
        if self.embedder is None:
            raise RuntimeError("correction_embedder_unavailable")

        previous_material = read_persisted_index_material(current)
        if previous_material is None:
            try:
                previous_material, _needs_persist = resolve_index_material(
                    current,
                    model_name=self._embedding_model_name(self.embedder),
                )
            except Exception as exc:
                raise RuntimeError("correction_index_material_unavailable") from exc
        if effective_embedding_model_name(self.embedder) != previous_material.model_name:
            raise RuntimeError("correction_embedding_model_mismatch")

        category = str(current.get("category") or "other")
        tier = str(current.get("tier") or "L1")
        domain = str(current.get("domain") or "uncategorized")
        l0_abstract = self._safe_summary_text(normalized_content)
        l1_summary = f"- {l0_abstract}" if l0_abstract else ""
        index_fields = {
            "content": normalized_content,
            "raw_content": normalized_content,
            "l0_abstract": l0_abstract,
            "l1_summary": l1_summary,
            "l2_content": normalized_content,
            "embedding_text": "\n".join(
                part
                for part in (
                    f"L0: {l0_abstract}" if l0_abstract else "",
                    f"L1: {l1_summary}" if l1_summary else "",
                )
                if part
            ),
            "search_text": l0_abstract or normalized_content,
            "domain": domain,
            "category": category,
        }
        material = prepare_index_material(
            index_fields,
            embedder=self.embedder,
            policy=previous_material.policy,
            model_name=previous_material.model_name,
        )
        try:
            vector = tuple(float(value) for value in self.embedder.embed(material.vector_text))
        except Exception as exc:
            raise RuntimeError("correction_embedding_failed") from exc
        if not self._has_nonzero_vector(vector):
            raise RuntimeError("correction_embedding_failed")

        original_tags = current.get("tags")
        if not isinstance(original_tags, (list, tuple)):
            raise ValueError("correction_source_tags_invalid")
        blocked_states = {
            "conflict",
            "corrected",
            "deleted",
            "deprecated",
            "expired",
            "forgotten",
            "obsolete",
            "replaced",
            "rejected",
            "stale",
            "wrong",
        }
        tags: list[str] = []
        for value in original_tags:
            tag = str(value).strip()
            prefix, separator, state = tag.partition(":")
            if not tag or tag.casefold() == "decay:pending":
                continue
            if (
                separator
                and prefix.casefold() in {"lifecycle", "quality", "status"}
                and state.strip().casefold() in blocked_states
            ):
                continue
            tags.append(tag)

        extracted = {
            "category": category,
            "l0_abstract": l0_abstract,
            "l1_summary": l1_summary,
            "l2_content": normalized_content,
            "confidence": 0.5,
            "importance": float(current.get("importance", 0.7) or 0.7),
        }
        from plastic_promise.core.quality_gate import QualityGate

        gate_score = QualityGate().score(
            extracted=extracted,
            tags=tags,
            domain_hint=domain,
            created_at=None,
            tier=tier,
        )
        decision = QualityGate.decide(gate_score)
        if decision == "discard":
            raise RuntimeError("correction_quality_failed")

        metadata = metadata_with_index_material(current.get("metadata_json"), material)
        for key in (
            "lifecycle_status",
            "mark_as",
            "quality_flag",
            "quality_status",
            "state",
            "status",
        ):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip().casefold() in blocked_states:
                metadata.pop(key, None)
        for state in blocked_states:
            if metadata.get(state) is True:
                metadata.pop(state, None)
        metadata.update(
            {
                "category": category,
                "domain": domain,
                "gate_score": round(gate_score, 4),
                "l0_abstract": l0_abstract,
                "l1_summary": l1_summary,
                "l2_content": normalized_content,
                "quality": {"status": "current", "decision": decision},
                "raw_content": normalized_content,
            }
        )
        return PreparedMemory(
            content=normalized_content,
            category=category,
            tier=tier,
            tags=tuple(tags),
            vector=vector,
            index_material=material,
            metadata=MappingProxyType(metadata),
        )

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
        migration_outcomes: dict[str, dict[str, str]] = {}

        # Stage 1: raw → tagged (noise filter + keyword extraction)
        counts["raw→tagged"] = self._process_raw_to_tagged()

        # Stage 2: tagged → classified (大类: tier L1/L3 判定)
        counts["tagged→classified"] = self._process_tagged_to_classified()

        # Stage 3: classified → embedded (细分: batch embed 向量化)
        counts["classified→embedded"] = self._process_classified_to_embedded()

        # Stage 4: embedded → migrate to main pool
        counts["embedded→migrated"] = self._process_embedded_to_migrate(migration_outcomes)

        self._last_process = datetime.datetime.now().isoformat()
        return {
            "pipeline": counts,
            "migration_outcomes": migration_outcomes,
            "rejections": dict(self._rejections),
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
        return effective_embedding_model_name(embedder)

    @classmethod
    def _build_memory_index_fields(
        cls,
        raw_content: str,
        extracted: dict[str, Any] | None = None,
        embedder: Any = None,
        policy: str | None = None,
        domain: str = "uncategorized",
        category: str = "other",
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
        summary_embedding_text = "\n".join(part for part in parts if part).strip()
        model_name = cls._embedding_model_name(embedder)
        fields = {
            "content": l2_content,
            "raw_content": raw_content or "",
            "l0_abstract": l0_abstract,
            "l1_summary": l1_summary,
            "l2_content": l2_content,
            "embedding_text": summary_embedding_text,
            "search_text": l0_abstract or cls._safe_summary_text(l1_summary or l2_content),
            "domain": domain,
            "category": category,
        }
        material = prepare_index_material(
            fields,
            embedder=embedder,
            policy=policy
            or initial_index_policy(summary_index_enabled=cls._summary_index_enabled()),
            model_name=model_name,
        )
        fields.update(
            {
                "embedding_text": material.vector_text,
                "embedding_hash": material.embedding_hash,
                "search_text": material.search_text,
            }
        )
        fields.pop("content")
        return fields

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
        for _mid, record in items:
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
            embed_texts = [r.get("embedding_text") or r["content"] for _, r in batch]
            # Skip Ollama embed for items marked skip_embed (e.g. auto_inject structured content)
            skip_set = {mid for mid, r in batch if r.get("skip_embed")}
            try:
                if skip_set:
                    vectors = []
                    for (mid, _record), embed_text in zip(batch, embed_texts, strict=True):
                        if mid in skip_set:
                            vectors.append([0.0] * self.embedder.dim)
                        else:
                            vectors.append(self.embedder.embed(embed_text))
                else:
                    vectors = self.embedder.embed_batch(embed_texts)
            except Exception as e:
                if self._is_terminal_embed_error(e):
                    # A batch can contain both valid and oversized sources.
                    # Retry item-by-item so one deterministic rejection does
                    # not defer otherwise healthy records forever.
                    for mid, record in batch:
                        try:
                            embed_text = record.get("embedding_text") or record["content"]
                            vec = (
                                [0.0] * self.embedder.dim
                                if mid in skip_set
                                else self.embedder.embed(embed_text)
                            )
                        except Exception as item_error:
                            if self._is_terminal_embed_error(item_error):
                                self._reject_embedding(mid, record, item_error)
                            else:
                                self._defer_embedding(record)
                            continue
                        if self.rec_mem is None and vec and not any(v != 0.0 for v in vec):
                            self._defer_embedding(record)
                            continue
                        record["vector"] = vec
                        record["stage"] = "embedded"
                        record["processed_at"] = datetime.datetime.now().isoformat()
                        count += 1
                    continue
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
            for (mid, record), vec in zip(batch, vectors, strict=True):
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

    @staticmethod
    def _is_terminal_embed_error(error: Exception) -> bool:
        return str(error).strip().casefold() in _TERMINAL_EMBED_ERRORS

    def _defer_embedding(self, record: dict[str, Any]) -> None:
        tags = record.setdefault("tags", [])
        if "embed:deferred" not in tags:
            tags.append("embed:deferred")

    def _reject_embedding(
        self,
        memory_id: str,
        record: dict[str, Any],
        error: Exception,
    ) -> None:
        reason = str(error).strip() or "embedding_rejected"
        self._rejections[memory_id] = {"reason": reason}
        record.setdefault("tags", []).append("embed:rejected")
        record["rejection_reason"] = reason
        record["stage"] = "rejected"
        self._buffer.pop(memory_id, None)

    def _process_embedded_to_migrate(
        self, migration_outcomes: dict[str, dict[str, str]] | None = None
    ) -> int:
        """Stage 4: Dedup → QualityGate → Migrate embedded items to main pool.

        Direction B enhancements:
          1. Vector dedup via LanceDB.check_duplicate() (cos >= 0.85 -> bump counters)
          2. QualityGate composite scoring (4-dim x 0.25)
          3. Normal RecMem.store() for passing records
        """
        from plastic_promise.core.quality_gate import QualityGate

        items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "embedded"]
        migration_outcomes = migration_outcomes if migration_outcomes is not None else {}
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
                            runtime = getattr(engine, "_memories", {})
                            existing = runtime.get(dup_id) if isinstance(runtime, dict) else None
                            py_rec = getattr(self.rec_mem, "_records", {}).get(dup_id)
                            candidate_type = (
                                existing.get("memory_type")
                                if isinstance(existing, dict)
                                else getattr(py_rec, "memory_type", None)
                            )
                            if engine_memory_is_governed_synthesis(
                                engine,
                                dup_id,
                                memory_type=candidate_type,
                            ):
                                logging.info(
                                    "Dedup ignored governed synthesis candidate %s for %s",
                                    dup_id,
                                    mid,
                                )
                            else:
                                now_iso = datetime.datetime.now().isoformat()
                                reinforce = getattr(
                                    engine,
                                    "reinforce_ordinary_duplicate",
                                    None,
                                )
                                canonical = None
                                updated = False
                                if callable(reinforce):
                                    result = reinforce(
                                        dup_id,
                                        entity_ids=list(record.get("entity_ids", []) or []),
                                        last_accessed=now_iso,
                                        expected_project_id=record.get(
                                            "project_id", "project:legacy-global"
                                        ),
                                        expected_visibility=record.get("visibility", "project"),
                                        expected_source_class=record.get(
                                            "source_class", "experience"
                                        ),
                                        expected_memory_type=record.get(
                                            "memory_type", "experience"
                                        ),
                                    )
                                    if isinstance(result, Mapping):
                                        canonical = result
                                        updated = True
                                if updated:
                                    if py_rec is not None:
                                        py_rec.access_count = canonical["access_count"]
                                        py_rec.worth_success = canonical["worth_success"]
                                        py_rec.last_accessed = canonical["last_accessed"]
                                        py_rec.entity_ids = list(canonical["entity_ids"])
                                        py_rec.effective_half_life = canonical.get(
                                            "effective_half_life",
                                            py_rec.effective_half_life,
                                        )
                                    migration_outcomes[mid] = {
                                        "status": "deduplicated",
                                        "canonical_memory_id": dup_id,
                                    }
                                    del self._buffer[mid]
                                    logging.info(
                                        "Dedup: %s -> merged into %s (similarity >= 0.85)",
                                        mid,
                                        dup_id,
                                    )
                                    continue  # skip store only after the guarded update succeeds
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
                if (
                    extracted_category == "other" or extracted_confidence < 0.5
                ) and "llm_pending:true" not in tags:
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
                        "search_text": record.get("search_text", ""),
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
                except Exception:
                    pass  # Graceful: decay init is a quality improvement

                # ---- Existing: vector + tags + domain persistence ----
                if engine is not None:
                    update_memory_fields = getattr(engine, "update_memory_fields", None)
                    if has_nonzero_vector:
                        ldb = getattr(engine, "lancedb_store", None)
                        ensure_heavy_init = getattr(engine, "ensure_heavy_init", None)
                        if ldb is None and callable(ensure_heavy_init):
                            try:
                                ensure_heavy_init()
                            except Exception as e:
                                logging.warning(
                                    "LanceDB init before dual-write failed for %s: %s",
                                    stored.memory_id,
                                    e,
                                )
                            ldb = getattr(engine, "lancedb_store", None)
                        if ldb is not None:
                            try:
                                lancedb_text = record.get("search_text") or record.get(
                                    "content", ""
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
                    if callable(update_memory_fields):
                        update_memory_fields(
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
                            raw_content=record.get("raw_content", ""),
                            l0_abstract=record.get("l0_abstract", ""),
                            l1_summary=record.get("l1_summary", ""),
                            l2_content=record.get("l2_content", ""),
                            embedding_text=record.get("embedding_text", ""),
                            embedding_hash=record.get("embedding_hash", ""),
                            search_text=record.get("search_text", ""),
                        )

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
                migration_outcomes[mid] = {
                    "status": "stored",
                    "canonical_memory_id": stored.memory_id,
                }
                count += 1
            except Exception as e:
                logging.warning("Migrate failed for %s: %s", mid, e)

        return count
