"""Tests for pipeline quality features — extraction, dedup, QualityGate integration."""

import datetime
from unittest.mock import ANY, MagicMock, patch

import pytest

from plastic_promise.core.embedder import FallbackEmbedder
from plastic_promise.memory.pipeline import MemoryPipeline
from plastic_promise.memory.soul_memory import MemoryRecord, RecMem


class TestPipelineQuality:
    """Integration tests for quality pipeline features."""

    class RecordingEmbedder:
        dim = 1024

        def __init__(self):
            self.texts = []
            self.model_name = "recording-test"

        def embed(self, text):
            self.texts.append(text)
            return [0.1] * self.dim

        def embed_batch(self, texts):
            self.texts.extend(texts)
            return [[0.1] * self.dim for _ in texts]

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
        with patch("plastic_promise.smart_extractor.extract_memories") as mock_extract:
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

    def test_oversized_structure_embedding_is_rejected_without_retry_loop(self):
        class OversizedEmbedder:
            dim = 4

            def __init__(self):
                self.batch_calls = 0
                self.embed_calls = []

            def embed_batch(self, texts):
                self.batch_calls += 1
                raise ValueError("structure_chunking_source_too_large")

            def embed(self, text):
                self.embed_calls.append(text)
                if len(text) > 8:
                    raise ValueError("structure_chunking_source_too_large")
                return [1.0] * self.dim

        embedder = OversizedEmbedder()
        pipeline = MemoryPipeline(rec_mem=None, embedder=embedder)
        pipeline._buffer = {
            "short": {"stage": "classified", "content": "short", "tags": []},
            "oversized": {
                "stage": "classified",
                "content": "x" * 32,
                "tags": [],
            },
        }

        assert pipeline._process_classified_to_embedded() == 1
        assert pipeline._buffer["short"]["stage"] == "embedded"
        assert "oversized" not in pipeline._buffer
        assert pipeline._rejections == {
            "oversized": {"reason": "structure_chunking_source_too_large"}
        }

        # A later maintenance cycle must not attempt the rejected item again.
        assert pipeline._process_classified_to_embedded() == 0
        assert embedder.batch_calls == 1
        assert embedder.embed_calls == ["short", "x" * 32]

    def test_store_urgent_builds_summary_index_fields_from_extraction(self, monkeypatch):
        monkeypatch.setenv("PP_MEMORY_SUMMARY_INDEX", "1")
        raw = "User said they like Rust, with extra source wording."
        l2 = "User likes Rust for backend development."
        with patch("plastic_promise.smart_extractor.extract_memories") as mock_extract:
            from plastic_promise.smart_extractor import ExtractedMemory

            mock_extract.return_value = [
                ExtractedMemory(
                    category="preference",
                    l0_abstract="User Rust preference",
                    l1_summary="- Language: Rust\n- Use: backend",
                    l2_content=l2,
                    importance=0.8,
                    confidence=0.9,
                    source_segment=raw,
                )
            ]
            mid = self.pipeline.store_urgent(raw)

        record = self.pipeline._buffer[mid]
        assert record["content"] == l2
        assert record["raw_content"] == raw
        assert record["l0_abstract"] == "User Rust preference"
        assert record["l1_summary"] == "- Language: Rust\n- Use: backend"
        assert record["l2_content"] == l2
        assert "L0: User Rust preference" in record["embedding_text"]
        assert "L1: - Language: Rust" in record["embedding_text"]
        assert "L2:" not in record["embedding_text"]
        assert "backend development" not in record["embedding_text"]
        assert record["search_text"] == "User Rust preference"
        assert len(record["embedding_hash"]) == 64
        assert record["metadata_json"]["extracted"]["l0_abstract"] == "User Rust preference"
        assert record["metadata_json"]["memory_index"] == {
            "embedding_hash": record["embedding_hash"],
            "hash_schema": "policy-model-text-v2",
            "model_name": "fallback-zero",
            "policy": "summary-v1",
            "search_text_hash": record["metadata_json"]["memory_index"][
                "search_text_hash"
            ],
        }

    @pytest.mark.parametrize(
        ("explicit", "summary_enabled", "expected"),
        [
            (None, False, "legacy"),
            (None, True, "summary-v1"),
            ("legacy", True, "legacy"),
            ("compact-v2", False, "compact-v2"),
        ],
    )
    def test_index_policy_precedence_preserves_unset_summary_compatibility(
        self, monkeypatch, explicit, summary_enabled, expected
    ):
        from plastic_promise.core.memory_index import initial_index_policy

        if explicit is None:
            monkeypatch.delenv("PP_MEMORY_INDEX_TEXT_POLICY", raising=False)
        else:
            monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", explicit)

        assert initial_index_policy(summary_index_enabled=summary_enabled) == expected

    def test_unknown_index_policy_fails_before_extraction_or_buffer_mutation(
        self, monkeypatch
    ):
        monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "compact-latest")
        self.pipeline._buffer["existing"] = {"stage": "raw"}

        with (
            patch("plastic_promise.smart_extractor.extract_memories") as extractor,
            pytest.raises(ValueError, match="unsupported_index_policy"),
        ):
            self.pipeline.store_urgent("must fail closed")

        extractor.assert_not_called()
        assert self.pipeline._buffer == {"existing": {"stage": "raw"}}

    def test_compact_v2_is_bounded_deterministic_and_preserves_bilingual_identifiers(
        self, monkeypatch
    ):
        from plastic_promise.core.memory_index import build_index_material

        monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "compact-v2")
        record = {
            "domain": "Building",
            "category": "Decision",
            "l0_abstract": "SQLite 是 canonical truth",
            "l1_summary": (
                "SQLite 是 canonical truth\n"
                "Use PP_SYNTHESIS_RETRIEVAL=0 and Ticket-ID_42.\n"
                "保留 CamelCase/API_v2 标识符。\n"
                "保留 CamelCase/API_v2 标识符。\n"
                + "line-a "
                + "A" * 540
                + "\nline-b "
                + "B" * 700
            ),
            "raw_content": "RAW-SECRET-MUST-NOT-APPEAR",
            "l2_content": "L2-SECRET-MUST-NOT-APPEAR",
            "content": "RAW-SECRET-MUST-NOT-APPEAR",
        }

        first = build_index_material(record, model_name="Model-X")
        second = build_index_material(record, model_name="Model-X")

        assert first == second
        assert first.policy == "compact-v2"
        assert first.vector_text.splitlines()[:3] == [
            "domain/category: Building / Decision",
            "L0: SQLite 是 canonical truth",
            "L1: Use PP_SYNTHESIS_RETRIEVAL=0 and Ticket-ID_42.",
        ]
        assert first.vector_text.count("SQLite 是 canonical truth") == 1
        assert "L1: 保留 CamelCase/API_v2 标识符。" in first.vector_text
        assert first.vector_text.count("L1: 保留 CamelCase/API_v2 标识符。") == 1
        assert "RAW-SECRET" not in first.vector_text
        assert "L2-SECRET" not in first.vector_text
        assert len(first.vector_text) <= 1200
        assert all(len(line) <= 400 for line in first.vector_text.splitlines())
        assert first.search_text == first.vector_text

    def test_compact_v2_long_single_line_retains_bounded_nonempty_information(self):
        from plastic_promise.core.memory_index import (
            COMPACT_V2_POLICY,
            build_index_material,
        )

        identifier = "PP_IDENTIFIER_X9"
        long_line = " ".join(
            ["SQLite", "中文", identifier] + [f"token{index:03d}" for index in range(400)]
        )
        material = build_index_material(
            {
                "l0_abstract": long_line,
                "l1_summary": long_line,
            },
            policy=COMPACT_V2_POLICY,
            model_name="Model-A",
        )

        assert 0 < len(material.vector_text) <= 1200
        assert "SQLite" in material.vector_text
        assert "中文" in material.vector_text
        assert identifier in material.vector_text
        assert material.vector_text.count(identifier) == 1
        assert all(len(line) <= 400 for line in material.vector_text.splitlines())

    @pytest.mark.parametrize("old_policy", ["compact-v2", "future-policy"])
    def test_pre_v2_materialization_rejects_nonlegacy_persisted_policy(
        self, old_policy
    ):
        from plastic_promise.core.memory_index import (
            IndexMaterialError,
            resolve_index_material,
        )

        with pytest.raises(IndexMaterialError, match="index_material_policy"):
            resolve_index_material(
                {
                    "content": "raw content must not launder policy",
                    "embedding_text": "old vector text",
                    "search_text": "old search text",
                    "embedding_hash": "old-hash",
                    "metadata_json": {
                        "memory_index": {
                            "policy": old_policy,
                            "embedding_hash": "old-hash",
                        }
                    },
                },
                model_name="Model-A",
            )

    def test_future_hash_schema_never_falls_back_to_pre_v2_materialization(self):
        from plastic_promise.core.memory_index import (
            IndexMaterialError,
            resolve_index_material,
        )

        with pytest.raises(IndexMaterialError, match="index_material_hash_schema_unknown"):
            resolve_index_material(
                {
                    "content": "raw content must not launder a future schema",
                    "embedding_text": "future vector",
                    "search_text": "future search",
                    "embedding_hash": "future-hash",
                    "metadata_json": {
                        "memory_index": {
                            "policy": "legacy",
                            "embedding_hash": "future-hash",
                            "hash_schema": "policy-model-text-v3",
                            "model_name": "Model-A",
                        }
                    },
                },
                model_name="Model-A",
            )

    def test_embedding_hash_binds_policy_effective_model_and_exact_vector_text(self):
        from plastic_promise.core.memory_index import (
            LEGACY_FALLBACK_POLICY,
            LEGACY_POLICY,
            build_index_material,
        )

        record = {"content": "Exact Vector Text"}
        baseline = build_index_material(
            record,
            policy=LEGACY_POLICY,
            model_name="Model-A",
        )
        assert baseline == build_index_material(
            record,
            policy=LEGACY_POLICY,
            model_name="Model-A",
        )
        assert baseline.vector_text == build_index_material(
            record,
            policy=LEGACY_FALLBACK_POLICY,
            model_name="Model-A",
        ).vector_text
        assert baseline.embedding_hash != build_index_material(
            record,
            policy=LEGACY_FALLBACK_POLICY,
            model_name="Model-A",
        ).embedding_hash
        assert baseline.embedding_hash != build_index_material(
            record,
            policy=LEGACY_POLICY,
            model_name="Model-B",
        ).embedding_hash
        assert baseline.embedding_hash != build_index_material(
            {"content": "Exact Vector Text!"},
            policy=LEGACY_POLICY,
            model_name="Model-A",
        ).embedding_hash

    def test_persisted_material_detects_vector_search_policy_and_model_drift(self):
        from plastic_promise.core.memory_index import (
            COMPACT_V2_POLICY,
            build_index_material,
            index_metadata,
            read_persisted_index_material,
        )

        material = build_index_material(
            {
                "domain": "building",
                "category": "decision",
                "l0_abstract": "SQLite truth",
                "l1_summary": "Keep PP_INDEX=1",
            },
            policy=COMPACT_V2_POLICY,
            model_name="Model-A",
        )
        row = {
            "embedding_text": material.vector_text,
            "search_text": material.search_text,
            "embedding_hash": material.embedding_hash,
            "metadata_json": {"memory_index": index_metadata(material)},
        }
        assert read_persisted_index_material(row, model_name="Model-A") == material
        assert read_persisted_index_material(
            {**row, "embedding_text": material.vector_text + "!"},
            model_name="Model-A",
        ) is None
        assert read_persisted_index_material(
            {**row, "search_text": material.search_text + "!"},
            model_name="Model-A",
        ) is None
        assert read_persisted_index_material(row, model_name="Model-B") is None
        changed_policy = {
            **row,
            "metadata_json": {
                "memory_index": {
                    **index_metadata(material),
                    "policy": "legacy",
                }
            },
        }
        assert read_persisted_index_material(changed_policy, model_name="Model-A") is None

    def test_pipeline_persists_compact_v2_exact_material(self, monkeypatch):
        from plastic_promise.core.memory_index import read_persisted_index_material
        from plastic_promise.smart_extractor import ExtractedMemory

        monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "compact-v2")
        embedder = self.RecordingEmbedder()
        pipeline = MemoryPipeline(rec_mem=self.rec_mem, embedder=embedder)
        with patch(
            "plastic_promise.smart_extractor.extract_memories",
            return_value=[
                ExtractedMemory(
                    category="decision",
                    l0_abstract="SQLite 是 truth",
                    l1_summary="Use PP_MEMORY_INDEX_TEXT_POLICY=compact-v2",
                    l2_content="RAW-L2-MUST-STAY-CANONICAL-ONLY",
                    importance=0.9,
                    confidence=0.95,
                    source_segment="raw source",
                )
            ],
        ):
            memory_id = pipeline.store_urgent(
                "raw source",
                domain_hint="building",
            )

        row = pipeline._buffer[memory_id]
        material = read_persisted_index_material(
            row,
            model_name=embedder.model_name,
        )
        assert material is not None
        assert material.policy == "compact-v2"
        assert row["embedding_text"].startswith(
            "domain/category: building / decision\nL0: SQLite 是 truth"
        )
        assert "RAW-L2" not in row["embedding_text"]

    def test_compact_v2_pipeline_embeds_and_dual_writes_exact_persisted_material(
        self,
        monkeypatch,
    ):
        from plastic_promise.smart_extractor import ExtractedMemory

        monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "compact-v2")
        raw = "RAW-SOURCE-SENTINEL-MUST-STAY-CANONICAL"
        l2 = "L2-SENTINEL-MUST-STAY-CANONICAL"
        embedder = self.RecordingEmbedder()
        rec_mem = MagicMock(spec=RecMem)
        rec_mem._records = {}
        engine = MagicMock()
        engine._memories = {"stored_compact_v2": {}}
        engine._sqlite = None
        warm_lancedb = MagicMock()
        engine.lancedb_store = None

        def ensure_heavy_init():
            engine.lancedb_store = warm_lancedb

        engine.ensure_heavy_init.side_effect = ensure_heavy_init
        rec_mem._engine = engine
        persisted_store_kwargs = {}

        def mock_store(**kwargs):
            persisted_store_kwargs.update(kwargs)
            memory = MemoryRecord(
                content=kwargs["content"],
                memory_type=kwargs["memory_type"],
                source=kwargs["source"],
                memory_id="stored_compact_v2",
                metadata_json=kwargs.get("metadata_json", {}),
            )
            rec_mem._records[memory.memory_id] = memory
            return memory

        rec_mem.store = mock_store
        pipeline = MemoryPipeline(rec_mem=rec_mem, embedder=embedder)
        pipeline._lancedb = MagicMock()
        pipeline._lancedb.check_duplicate.return_value = None

        with patch(
            "plastic_promise.smart_extractor.extract_memories",
            return_value=[
                ExtractedMemory(
                    category="fact",
                    l0_abstract="Compact persisted fact",
                    l1_summary="Identifier_X1 remains searchable",
                    l2_content=l2,
                    importance=0.9,
                    confidence=0.95,
                    source_segment=raw,
                )
            ],
        ):
            memory_id = pipeline.store_urgent(raw, domain_hint="building")

        buffered = dict(pipeline._buffer[memory_id])
        expected_vector_text = buffered["embedding_text"]
        expected_search_text = buffered["search_text"]
        pipeline.process_pipeline()

        assert embedder.texts == [expected_vector_text]
        assert persisted_store_kwargs["metadata_json"]["embedding_text"] == (
            expected_vector_text
        )
        assert persisted_store_kwargs["metadata_json"]["search_text"] == (
            expected_search_text
        )
        persist_call = next(
            call
            for call in engine.update_memory_fields.call_args_list
            if "embedding_text" in call.kwargs
        )
        assert persist_call.kwargs["embedding_text"] == expected_vector_text
        assert persist_call.kwargs["search_text"] == expected_search_text
        assert warm_lancedb.insert.call_args.kwargs["text"] == expected_search_text
        assert raw not in expected_vector_text
        assert l2 not in expected_vector_text
        assert raw not in expected_search_text
        assert l2 not in expected_search_text

    def test_store_urgent_rejects_synthesis_before_extraction_or_buffer_mutation(self):
        self.pipeline._buffer["existing"] = {"stage": "raw"}

        with (
            patch("plastic_promise.smart_extractor.extract_memories") as mock_extract,
            pytest.raises(RuntimeError, match="synthesis_requires_governed_store"),
        ):
            self.pipeline.store_urgent("must be governed", memory_type="synthesis")

        mock_extract.assert_not_called()
        assert self.pipeline._buffer == {"existing": {"stage": "raw"}}

    def test_store_urgent_captures_index_policy_once(self):
        with (
            patch.object(
                MemoryPipeline,
                "_summary_index_enabled",
                side_effect=[True, False],
            ) as enabled,
            patch("plastic_promise.smart_extractor.extract_memories", return_value=[]),
        ):
            mid = self.pipeline.store_urgent("atomic policy content")

        record = self.pipeline._buffer[mid]
        assert enabled.call_count == 1
        assert record["metadata_json"]["memory_index"] == {
            "embedding_hash": record["embedding_hash"],
            "hash_schema": "policy-model-text-v2",
            "model_name": "fallback-zero",
            "policy": "summary-v1",
            "search_text_hash": record["metadata_json"]["memory_index"][
                "search_text_hash"
            ],
        }
        assert record["embedding_text"].startswith("L0: atomic policy content")

    def test_store_urgent_no_extraction_returns_none(self):
        """extract_memories returns empty and content is whitespace → returns None."""
        with patch("plastic_promise.smart_extractor.extract_memories") as mock_extract:
            mock_extract.return_value = []
            result = self.pipeline.store_urgent("   ")
            assert result is None

    def test_store_urgent_extraction_error_fallback(self):
        """extract_memories raises → fall back to raw content, no extracted field."""
        with patch("plastic_promise.smart_extractor.extract_memories") as mock_extract:
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
        mid = "fuzzy_testdup"
        self.pipeline._buffer[mid] = {
            "memory_id": mid,
            "content": "duplicate content",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:legacy-global",
            "visibility": "project",
            "source_class": "experience",
            "stage": "embedded",
            "tags": [],
            "domain": "uncategorized",
            "vector": [0.5] * 1024,
            "extracted": {"category": "fact", "confidence": 0.8},
            "entity_ids": [],
            "created_at": datetime.datetime.now().isoformat(),
        }
        # Mock engine internals
        self.pipeline.rec_mem._engine = MagicMock()
        self.pipeline.rec_mem._engine._memories = {"existing_001": {}}
        self.pipeline.rec_mem._engine._sqlite = None
        self.pipeline.rec_mem._engine.reinforce_ordinary_duplicate.return_value = {
            "access_count": 1,
            "worth_success": 1,
            "entity_ids": [],
            "last_accessed": "2026-07-12T00:00:00",
            "effective_half_life": 3.0,
        }

        result = self.pipeline.process_pipeline()

        # Buffer entry should be removed
        assert mid not in self.pipeline._buffer
        # rec_mem.store should NOT have been called
        self.rec_mem.store.assert_not_called()
        self.pipeline.rec_mem._engine.reinforce_ordinary_duplicate.assert_called_once_with(
            "existing_001",
            entity_ids=[],
            last_accessed=ANY,
            expected_project_id="project:legacy-global",
            expected_visibility="project",
            expected_source_class="experience",
            expected_memory_type="experience",
        )
        assert result["migration_outcomes"][mid] == {
            "status": "deduplicated",
            "canonical_memory_id": "existing_001",
        }

    def test_migrate_reinforces_duplicate_when_second_engine_cache_is_stale(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Canonical duplicate reinforcement must not depend on a hot cache entry."""
        from plastic_promise.core.context_engine import ContextEngine

        db_path = tmp_path / "canonical.db"
        monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
        writer = ContextEngine(use_sqlite=True)
        reader = ContextEngine(use_sqlite=True)
        duplicate_id = "ordinary-stale-pipeline-duplicate"
        incoming_id = "incoming-stale-pipeline-duplicate"
        try:
            assert writer.register_memory(
                {
                    "id": duplicate_id,
                    "content": "canonical duplicate evidence",
                    "memory_type": "experience",
                    "source": "test",
                    "project_id": "project:dedup",
                    "visibility": "project",
                    "source_class": "experience",
                }
            ) == duplicate_id
            assert duplicate_id not in reader._memories

            rec_mem = MagicMock(spec=RecMem)
            rec_mem._engine = reader
            rec_mem._records = {}
            pipeline = MemoryPipeline(rec_mem=rec_mem, embedder=self.embedder)
            lancedb = MagicMock()
            lancedb.check_duplicate.return_value = duplicate_id
            pipeline._lancedb = lancedb
            pipeline._buffer[incoming_id] = {
                "memory_id": incoming_id,
                "content": "incoming duplicate evidence",
                "memory_type": "experience",
                "source": "test",
                "project_id": "project:dedup",
                "visibility": "project",
                "source_class": "experience",
                "stage": "embedded",
                "tags": [],
                "domain": "building",
                "vector": [0.5] * 1024,
                "entity_ids": ["entity:stale-cache"],
                "extracted": {"category": "fact", "confidence": 0.9},
                "created_at": "2026-07-12T00:00:00",
            }

            pipeline._process_embedded_to_migrate()

            canonical = writer._sqlite.get(duplicate_id)
            assert incoming_id not in pipeline._buffer
            rec_mem.store.assert_not_called()
            assert canonical["access_count"] == 1
            assert canonical["worth_success"] == 1
            assert canonical["entity_ids"] == ["entity:stale-cache"]
            assert reader._memories[duplicate_id] == canonical
        finally:
            reader._sqlite._conn.close()
            writer._sqlite._conn.close()

    def test_migrate_stores_incoming_when_duplicate_is_cross_project(
        self,
        tmp_path,
        monkeypatch,
    ):
        from plastic_promise.core.context_engine import ContextEngine

        db_path = tmp_path / "cross-project.db"
        monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
        writer = ContextEngine(use_sqlite=True)
        reader = ContextEngine(use_sqlite=True)
        duplicate_id = "project-a-duplicate"
        incoming_id = "project-b-incoming"
        try:
            writer.register_memory(
                {
                    "id": duplicate_id,
                    "content": "project A canonical evidence",
                    "memory_type": "experience",
                    "source": "test",
                    "project_id": "project:a",
                    "visibility": "project",
                    "source_class": "experience",
                }
            )
            before = writer._sqlite.get(duplicate_id)

            rec_mem = RecMem(reader)
            reader.ensure_heavy_init = MagicMock()
            pipeline = MemoryPipeline(rec_mem=rec_mem, embedder=self.embedder)
            lancedb = MagicMock()
            lancedb.check_duplicate.return_value = duplicate_id
            pipeline._lancedb = lancedb
            pipeline._buffer[incoming_id] = {
                "memory_id": incoming_id,
                "content": "project B similar but isolated evidence",
                "memory_type": "experience",
                "source": "test",
                "project_id": "project:b",
                "visibility": "project",
                "source_class": "experience",
                "stage": "embedded",
                "tags": [],
                "domain": "building",
                "vector": [0.5] * 1024,
                "entity_ids": ["entity:project-b"],
                "extracted": {"category": "fact", "confidence": 0.9},
                "created_at": "2026-07-12T00:00:00",
            }

            outcomes = {}
            pipeline._process_embedded_to_migrate(outcomes)

            assert incoming_id not in pipeline._buffer
            assert outcomes[incoming_id] == {
                "status": "stored",
                "canonical_memory_id": incoming_id,
            }
            assert writer._sqlite.get(duplicate_id) == before
            stored = writer._sqlite.get(incoming_id)
            assert stored["project_id"] == "project:b"
            assert stored["entity_ids"] == ["entity:project-b"]
        finally:
            reader._sqlite._conn.close()
            writer._sqlite._conn.close()

    @pytest.mark.parametrize("reservation_kind", ["type", "control"])
    def test_dedup_hit_on_governed_synthesis_preserves_incoming_evidence(
        self,
        tmp_path,
        reservation_kind,
    ):
        from plastic_promise.core.context_engine import ContextEngine, _SQLiteStorage

        storage = _SQLiteStorage(str(tmp_path / f"dedup-{reservation_kind}.db"))
        governed_id = "governed-dedup"
        governed = {
            "id": governed_id,
            "content": "governed synthesis candidate must remain byte-identical",
            "memory_type": "synthesis" if reservation_kind == "type" else "experience",
            "source": "synthesis",
            "source_class": "synthesis",
            "access_count": 7,
            "worth_success": 3,
            "worth_failure": 1,
        }
        storage.upsert(governed_id, governed)
        if reservation_kind == "control":
            storage._conn.execute(
                "INSERT INTO synthesis_artifacts "
                "(memory_id, synthesis_key, status, metadata_json, created_at, updated_at) "
                "VALUES (?, ?, 'draft', '{}', ?, ?)",
                (governed_id, "dedup:control", "2026-07-10", "2026-07-10"),
            )
            storage._conn.commit()
        before = storage.get(governed_id)
        control_before = storage._conn.execute(
            "SELECT * FROM synthesis_artifacts WHERE memory_id = ?", (governed_id,)
        ).fetchone()

        engine = ContextEngine(use_sqlite=False)
        engine._sqlite = storage
        engine._memories = dict(storage.iter_all())
        engine._loaded_memory_version = storage._conn.execute(
            "SELECT version FROM memory_version"
        ).fetchone()[0]
        engine.canonical_sync_ok = True
        lancedb = MagicMock()
        lancedb.check_duplicate.return_value = governed_id
        engine._ldb = lancedb
        updated_ids = []
        original_update = engine.update_memory_fields

        def record_update(memory_id, **fields):
            updated_ids.append(memory_id)
            return original_update(memory_id, **fields)

        engine.update_memory_fields = record_update
        rec_mem = MagicMock(spec=RecMem)
        rec_mem._engine = engine
        rec_mem._records = {}

        def store_incoming(**kwargs):
            engine.register_memory(
                {
                    "id": kwargs["memory_id"],
                    "content": kwargs["content"],
                    "memory_type": kwargs["memory_type"],
                    "source": kwargs["source"],
                    "tags": kwargs.get("tags", []),
                    "domain": kwargs.get("domain", "uncategorized"),
                    "metadata_json": kwargs.get("metadata_json", {}),
                }
            )
            record = MemoryRecord(
                content=kwargs["content"],
                memory_type=kwargs["memory_type"],
                source=kwargs["source"],
                memory_id=kwargs["memory_id"],
            )
            rec_mem._records[record.memory_id] = record
            return record

        rec_mem.store = store_incoming
        pipeline = MemoryPipeline(rec_mem=rec_mem, embedder=self.embedder)
        pipeline._lancedb = lancedb
        incoming_id = "incoming-evidence"
        pipeline._buffer[incoming_id] = {
            "memory_id": incoming_id,
            "content": "Independent ordinary evidence must survive a synthesis dedup hit.",
            "memory_type": "experience",
            "source": "user",
            "stage": "embedded",
            "tags": ["quality:verified"],
            "domain": "building",
            "tier": "L1",
            "vector": [0.5] * 1024,
            "entity_ids": [],
            "extracted": {
                "category": "fact",
                "confidence": 0.95,
                "l0_abstract": "Independent evidence",
                "l1_summary": "- Evidence remains ordinary",
                "l2_content": "Independent ordinary evidence must remain stored.",
            },
            "created_at": datetime.datetime.now().isoformat(),
        }

        assert pipeline._process_embedded_to_migrate() == 1
        assert storage.get(incoming_id) is not None
        assert storage.get(governed_id) == before
        assert storage._conn.execute(
            "SELECT * FROM synthesis_artifacts WHERE memory_id = ?", (governed_id,)
        ).fetchone() == control_before
        assert governed_id not in updated_ids
        storage._conn.close()

    def test_migrate_discards_low_quality(self):
        """QualityGate score < 0.3 → buffer entry discarded."""
        # Create entry with intentionally terrible extraction data
        mid = "fuzzy_testlow"
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
                # No category, no L0/L1/L2 — minimal info density
                "confidence": 0.0,  # zero confidence
            },
            "entity_ids": [],
            "created_at": "2026-06-17T12:00:00",  # 13 days old → freshness < 0.3
        }
        self.pipeline.rec_mem._engine = MagicMock()
        self.pipeline.rec_mem._engine._memories = {}

        self.pipeline._process_embedded_to_migrate()

        # Buffer entry should be removed (discarded)
        assert mid not in self.pipeline._buffer
        # rec_mem.store should NOT have been called
        self.rec_mem.store.assert_not_called()

    def test_migrate_dedup_updates_effective_half_life(self):
        """Gap 1 fix: Dedup hit recomputes effective_half_life via AccessReinforcement."""
        self.lancedb.check_duplicate.return_value = "existing_002"

        mid = "fuzzy_testboost"
        self.pipeline._buffer[mid] = {
            "memory_id": mid,
            "content": "reinforced duplicate",
            "memory_type": "experience",
            "source": "user",
            "stage": "embedded",
            "tags": [],
            "domain": "uncategorized",
            "vector": [0.5] * 1024,
            "entity_ids": [],
            "extracted": {"category": "fact", "confidence": 0.8},
            "created_at": datetime.datetime.now().isoformat(),
        }

        # Create a Python-side record with known tier and baseline half-life
        from plastic_promise.memory.soul_memory import MemoryRecord

        py_rec = MemoryRecord(
            content="existing content",
            memory_type="experience",
            source="user",
            memory_id="existing_002",
            tier="L3",
        )
        py_rec.access_count = 2
        py_rec.last_accessed = "2026-06-25T00:00:00"
        original_hl = py_rec.effective_half_life  # should be default 90.0 for L3

        self.pipeline.rec_mem._records["existing_002"] = py_rec
        self.pipeline.rec_mem._engine = MagicMock()
        self.pipeline.rec_mem._engine._memories = {
            "existing_002": {
                "access_count": 2,
                "worth_success": 1,
                "last_accessed": "2026-06-25T00:00:00",
            }
        }
        self.pipeline.rec_mem._engine._sqlite = None
        self.pipeline.rec_mem._engine.reinforce_ordinary_duplicate.return_value = {
            "access_count": 3,
            "worth_success": 2,
            "entity_ids": [],
            "last_accessed": "2026-06-30T12:00:00",
            "effective_half_life": original_hl + 1.0,
        }

        with patch("plastic_promise.memory.pipeline.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = __import__("datetime").datetime.fromisoformat(
                "2026-06-30T12:00:00"
            )
            mock_dt.datetime.now.isoformat = lambda: "2026-06-30T12:00:00"
            self.pipeline._process_embedded_to_migrate()

        # Buffer entry removed (dedup skip)
        assert mid not in self.pipeline._buffer
        # access_count incremented
        assert py_rec.access_count == 3
        # effective_half_life should be recomputed (boosted from access + recency)
        assert py_rec.effective_half_life != original_hl

    def test_migrate_store_initializes_decay_fields(self):
        """Gap 3 fix: After RecMem.store(), decay_multiplier and effective_half_life are set."""
        self.lancedb.check_duplicate.return_value = None  # no dedup

        mid = "fuzzy_testdecayinit"
        self.pipeline._buffer[mid] = {
            "memory_id": mid,
            "content": "memory that needs decay init",
            "memory_type": "experience",
            "source": "user",
            "stage": "embedded",
            "tags": ["test"],
            "domain": "building",
            "tier": "L3",
            "vector": [0.5] * 1024,
            "entity_ids": [],
            "extracted": {
                "category": "fact",
                "confidence": 0.85,
                "l0_abstract": "Test memory for decay initialization",
                "l1_summary": "[fact] Test memory should get decay fields set",
                "l2_content": "A sufficiently long content string that provides enough information density to pass the quality gate threshold comfortably.",
            },
            "created_at": datetime.datetime.now().isoformat(),
        }

        # Mock RecMem.store to return a real-like record and track it
        stored_records = {}

        def mock_store(**kwargs):
            from plastic_promise.memory.soul_memory import MemoryRecord

            mr = MemoryRecord(**kwargs)
            mr.memory_id = "stored_decay_init"
            stored_records[mr.memory_id] = mr
            self.pipeline.rec_mem._records[mr.memory_id] = mr
            return mr

        self.rec_mem.store = mock_store
        self.pipeline.rec_mem._engine = MagicMock()
        # RecMem.store() normally registers to engine — our mock bypasses that,
        # so pre-populate the engine dict with the expected ID
        self.pipeline.rec_mem._engine._memories = {"stored_decay_init": {}}
        self.pipeline.rec_mem._engine._sqlite = None

        self.pipeline._process_embedded_to_migrate()

        # Buffer entry removed (successful store)
        assert mid not in self.pipeline._buffer
        # The stored record should have decay_multiplier set (not default 1.0 for old dates)
        stored = stored_records.get("stored_decay_init")
        assert stored is not None
        # For a just-created memory, decay_multiplier should be close to 1.0
        assert stored.decay_multiplier > 0.9
        # effective_half_life should be the L3 base (90 days), not the default 3.0
        assert stored.effective_half_life > 3.0

    def test_summary_index_gate_embeds_summary_only_and_lancedb_uses_search_text(
        self, monkeypatch
    ):
        monkeypatch.setenv("PP_MEMORY_SUMMARY_INDEX", "1")
        embedder = self.RecordingEmbedder()
        rec_mem = MagicMock(spec=RecMem)
        rec_mem._records = {}
        engine = MagicMock()
        engine._memories = {"stored_summary": {}}
        engine._sqlite = None
        warm_lancedb = MagicMock()
        engine.lancedb_store = None

        def ensure_heavy_init():
            engine.lancedb_store = warm_lancedb

        engine.ensure_heavy_init.side_effect = ensure_heavy_init
        rec_mem._engine = engine

        def mock_store(**kwargs):
            mr = MemoryRecord(
                content=kwargs["content"],
                memory_type=kwargs["memory_type"],
                source=kwargs["source"],
                memory_id="stored_summary",
                metadata_json=kwargs.get("metadata_json", {}),
            )
            rec_mem._records[mr.memory_id] = mr
            return mr

        rec_mem.store = mock_store
        pipeline = MemoryPipeline(rec_mem=rec_mem, embedder=embedder)
        pipeline._lancedb = MagicMock()
        pipeline._lancedb.check_duplicate.return_value = None

        with patch("plastic_promise.smart_extractor.extract_memories") as mock_extract:
            from plastic_promise.smart_extractor import ExtractedMemory

            mock_extract.return_value = [
                ExtractedMemory(
                    category="fact",
                    l0_abstract="Compact LanceDB index text",
                    l1_summary="- Full detail preserved in SQL",
                    l2_content="Long SQL-only detail with exact identifier PP-12345.",
                    importance=0.8,
                    confidence=0.9,
                    source_segment="raw source with PP-12345",
                )
            ]
            pipeline.store_urgent("raw source with PP-12345")

        pipeline.process_pipeline()

        assert embedder.texts
        assert embedder.texts[0].startswith("L0: Compact LanceDB index text")
        assert "L1: - Full detail preserved in SQL" in embedder.texts[0]
        assert "L2:" not in embedder.texts[0]
        assert "PP-12345" not in embedder.texts[0]
        engine.ensure_heavy_init.assert_called()
        insert_kwargs = warm_lancedb.insert.call_args.kwargs
        assert insert_kwargs["text"] == "Compact LanceDB index text"
        assert "PP-12345" not in insert_kwargs["text"]
        stored_metadata = rec_mem._records["stored_summary"].metadata_json
        assert stored_metadata["raw_content"] == "raw source with PP-12345"
        assert stored_metadata["l2_content"] == "Long SQL-only detail with exact identifier PP-12345."

    def test_summary_index_gate_off_preserves_lancedb_content_text(self, monkeypatch):
        monkeypatch.delenv("PP_MEMORY_SUMMARY_INDEX", raising=False)
        embedder = self.RecordingEmbedder()
        rec_mem = MagicMock(spec=RecMem)
        rec_mem._records = {}
        engine = MagicMock()
        engine._memories = {"stored_legacy": {}}
        engine._sqlite = None
        engine.lancedb_store = MagicMock()
        rec_mem._engine = engine

        def mock_store(**kwargs):
            mr = MemoryRecord(
                content=kwargs["content"],
                memory_type=kwargs["memory_type"],
                source=kwargs["source"],
                memory_id="stored_legacy",
                metadata_json=kwargs.get("metadata_json", {}),
            )
            rec_mem._records[mr.memory_id] = mr
            return mr

        rec_mem.store = mock_store
        pipeline = MemoryPipeline(rec_mem=rec_mem, embedder=embedder)
        pipeline._lancedb = MagicMock()
        pipeline._lancedb.check_duplicate.return_value = None

        with patch("plastic_promise.smart_extractor.extract_memories") as mock_extract:
            mock_extract.return_value = []
            pipeline.store_urgent("Legacy full content text")

        pipeline.process_pipeline()

        assert embedder.texts == ["Legacy full content text"]
        insert_kwargs = engine.lancedb_store.insert.call_args.kwargs
        assert insert_kwargs["text"] == "Legacy full content text"

    def test_sqlite_round_trips_summary_index_fields(self, tmp_path):
        from plastic_promise.core.context_engine import _SQLiteMemoryStore

        db_path = str(tmp_path / "memory.db")
        store = _SQLiteMemoryStore(db_path)
        store.upsert(
            "summary_row",
            {
                "content": "display content",
                "memory_type": "experience",
                "source": "test",
                "raw_content": "raw source text",
                "l0_abstract": "compact index",
                "l1_summary": "- structured summary",
                "l2_content": "full narrative",
                "embedding_text": "L0: compact index\nL2: full narrative",
                "embedding_hash": "abc123",
                "search_text": "compact index",
                "metadata_json": {
                    "raw_content": "metadata fallback should not win",
                    "memory_index": {
                        "policy": "summary-v1",
                        "embedding_hash": "abc123",
                    },
                },
            },
        )

        row = store.get("summary_row")
        assert row["raw_content"] == "raw source text"
        assert row["l0_abstract"] == "compact index"
        assert row["l1_summary"] == "- structured summary"
        assert row["l2_content"] == "full narrative"
        assert row["embedding_text"] == "L0: compact index\nL2: full narrative"
        assert row["embedding_hash"] == "abc123"
        assert row["search_text"] == "compact index"
        assert row["metadata_json"]["memory_index"] == {
            "policy": "summary-v1",
            "embedding_hash": "abc123",
        }

        # Re-opening runs the migration again and must preserve both schema and data.
        reopened = _SQLiteMemoryStore(db_path)
        reopened_row = reopened.get("summary_row")
        assert reopened_row["search_text"] == "compact index"
        columns = {
            column[1] for column in reopened._conn.execute("PRAGMA table_info(memories)")
        }
        assert "search_text" in columns
        assert len(columns) == len(
            {column[1] for column in store._conn.execute("PRAGMA table_info(memories)")}
        )
