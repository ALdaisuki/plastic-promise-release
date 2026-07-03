import plastic_promise.smart_extractor as smart_extractor
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.lancedb_store import LanceDBStore
from plastic_promise.mcp.tools import memory as memory_tools


def _patch_rerank_identity(monkeypatch):
    from plastic_promise.core.reranker import MultiProviderReranker

    monkeypatch.setattr(MultiProviderReranker, "rerank", lambda self, query, items: items)


def _engine_with_result(monkeypatch, result, memory=None, vector_results=None):
    _patch_rerank_identity(monkeypatch)
    engine = ContextEngine(use_sqlite=False)
    engine._ensure_heavy_init = lambda: None
    engine._activate_principles = lambda task_type, task_description: []
    engine._inject_activated_to_graph = lambda activated, task_type: 0
    engine._graph_traversal = lambda task_type: []
    engine._text_retrieval = lambda query, trust_boost=1.0: []
    engine._vector_retrieval = lambda vector: vector_results or []
    engine._fts_retrieval = lambda query, scope="global": []
    engine._layered_fuse = lambda graph, text, vector: [result]
    engine._apply_edge_feedback = lambda: None
    engine._apply_decay_awareness = lambda score, mem, current_time, trust_boost: score
    engine._apply_mmr = lambda items, threshold=0.85, penalty=0.70: items
    engine._compute_divergent_quality = lambda items, all_items: items
    engine._calc_freshness = lambda item_id: "valid"
    engine._calc_decay_status = lambda item_id, mem: "healthy"
    engine._memories = {}
    if memory is not None:
        engine._memories[result[0]] = memory
    return engine


class TestFTSFusion:
    def test_fts_retrieval_returns_empty_when_ldb_is_none(self):
        engine = ContextEngine(use_sqlite=False)
        engine._ldb = None

        assert engine._fts_retrieval("query") == []

    def test_fts_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("PP_FTS_DISABLED", raising=False)
        monkeypatch.delenv("PP_FTS_FUSION", raising=False)

        class FakeLDB:
            def fts_search(self, query, k=20, scope=None):
                return [("m1", 0.9, "exact text", "L1", scope or "global")]

        engine = ContextEngine(use_sqlite=False)
        engine._ldb = FakeLDB()

        assert engine._fts_retrieval("exact") == [("m1", 0.9, "exact text", "fts")]

    def test_fts_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("PP_FTS_DISABLED", "1")

        class FakeLDB:
            def fts_search(self, query, k=20, scope=None):
                raise AssertionError("FTS should be skipped")

        engine = ContextEngine(use_sqlite=False)
        engine._ldb = FakeLDB()

        assert engine._fts_retrieval("exact") == []

    def test_fts_retrieval_passes_non_global_scope(self, monkeypatch):
        monkeypatch.delenv("PP_FTS_DISABLED", raising=False)
        monkeypatch.delenv("PP_FTS_FUSION", raising=False)
        seen = {}

        class FakeLDB:
            def fts_search(self, query, k=20, scope=None):
                seen["scope"] = scope
                return [("m1", 0.9, "exact text", "L1", scope)]

        engine = ContextEngine(use_sqlite=False)
        engine._ldb = FakeLDB()

        assert engine._fts_retrieval("exact", scope="designing") == [
            ("m1", 0.9, "exact text", "fts")
        ]
        assert seen["scope"] == "designing"

    def test_fts_retrieval_does_not_pass_global_scope(self, monkeypatch):
        monkeypatch.delenv("PP_FTS_DISABLED", raising=False)
        monkeypatch.delenv("PP_FTS_FUSION", raising=False)
        seen = {}

        class FakeLDB:
            def fts_search(self, query, k=20, scope=None):
                seen["scope"] = scope
                return [("m1", 0.9, "exact text", "L1", "global")]

        engine = ContextEngine(use_sqlite=False)
        engine._ldb = FakeLDB()

        engine._fts_retrieval("exact", scope="global")

        assert seen["scope"] is None

    def test_lancedb_fts_score_keeps_relevance_score(self):
        store = object.__new__(LanceDBStore)
        store._vectors_disabled = False
        store._fts_ready = True

        class FakeSearch:
            def limit(self, k):
                return self

            def to_list(self):
                return [
                    {
                        "memory_id": "m1",
                        "_score": 0.95,
                        "text": "exact lexical hit",
                        "tier": "L1",
                        "scope": "global",
                    }
                ]

        class FakeTable:
            def search(self, query, query_type=None):
                return FakeSearch()

        store._table = FakeTable()

        assert store.search_fts("exact")[0][1] == 0.95

    def test_lancedb_fts_distance_still_converts_to_similarity(self):
        store = object.__new__(LanceDBStore)
        store._vectors_disabled = False
        store._fts_ready = True

        class FakeSearch:
            def limit(self, k):
                return self

            def to_list(self):
                return [
                    {
                        "memory_id": "m1",
                        "_distance": 0.2,
                        "text": "distance hit",
                        "tier": "L1",
                        "scope": "global",
                    }
                ]

        class FakeTable:
            def search(self, query, query_type=None):
                return FakeSearch()

        store._table = FakeTable()

        assert store.search_fts("exact")[0][1] == 0.8


class TestHardThreshold:
    def test_score_below_hard_min_is_excluded(self, monkeypatch):
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0.50")
        engine = _engine_with_result(
            monkeypatch,
            ("low", 0.4, "useful enough content", "vector"),
            {"source": "user", "memory_type": "experience", "worth_success": 0, "worth_failure": 0},
        )

        pack = engine._supply_python("query", [0.0], debug=True)

        assert pack.total_items == 0
        assert pack.pipeline_stats["after_hard_score_filter"] == 0

    def test_score_above_hard_min_is_included(self, monkeypatch):
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0.50")
        engine = _engine_with_result(
            monkeypatch,
            ("high", 0.7, "useful enough content", "vector"),
            {"source": "user", "memory_type": "experience", "worth_success": 0, "worth_failure": 0},
        )

        pack = engine._supply_python("query", [0.0], debug=True)

        assert pack.total_items == 1
        assert pack.pipeline_stats["after_hard_score_filter"] == 1

    def test_hard_min_score_zero_disables(self, monkeypatch):
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        engine = _engine_with_result(
            monkeypatch,
            ("low", 0.25, "useful enough content", "vector"),
            {"source": "user", "memory_type": "experience", "worth_success": 0, "worth_failure": 0},
        )

        pack = engine._supply_python("query", [0.0], debug=True)

        assert pack.total_items == 1
        assert pack.divergent[0].id == "low"

    def test_score_below_hard_min_after_rerank_is_excluded(self, monkeypatch):
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0.50")

        from plastic_promise.core.reranker import MultiProviderReranker

        engine = _engine_with_result(
            monkeypatch,
            ("drops", 0.9, "useful enough content", "vector"),
            {"source": "user", "memory_type": "experience", "worth_success": 0, "worth_failure": 0},
        )
        monkeypatch.setattr(
            MultiProviderReranker,
            "rerank",
            lambda self, query, items: [setattr(item, "relevance", 0.4) or item for item in items],
        )

        pack = engine._supply_python("query", [0.0], debug=True)

        assert pack.total_items == 0


class TestSourceFilter:
    def test_daemon_source_is_downweighted(self, monkeypatch):
        monkeypatch.setenv("PP_SOURCE_FILTER", "1")
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        engine = _engine_with_result(
            monkeypatch,
            ("daemon", 1.0, "useful enough content", "vector"),
            {
                "source": "maintenance_daemon",
                "memory_type": "reflection",
                "worth_success": 0,
                "worth_failure": 0,
            },
        )

        pack = engine._supply_python("query", [0.0], debug=True)

        assert pack.total_items == 1
        assert pack.per_item_stats[0]["source_penalty"] == 0.3
        assert pack.per_item_stats[0]["final_score"] < 0.4

    def test_user_source_is_not_downweighted(self, monkeypatch):
        monkeypatch.setenv("PP_SOURCE_FILTER", "1")
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        engine = _engine_with_result(
            monkeypatch,
            ("user", 1.0, "useful enough content", "vector"),
            {"source": "user", "memory_type": "experience", "worth_success": 0, "worth_failure": 0},
        )

        pack = engine._supply_python("query", [0.0], debug=True)

        assert pack.total_items == 1
        assert pack.per_item_stats[0]["source_penalty"] == 1.0
        assert pack.per_item_stats[0]["final_score"] > 0.8

    def test_source_filter_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("PP_SOURCE_FILTER", "0")
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        engine = _engine_with_result(
            monkeypatch,
            ("daemon", 1.0, "useful enough content", "vector"),
            {
                "source": "maintenance_daemon",
                "memory_type": "reflection",
                "worth_success": 0,
                "worth_failure": 0,
            },
        )

        pack = engine._supply_python("query", [0.0], debug=True)

        assert pack.total_items == 1
        assert pack.per_item_stats[0]["source_penalty"] == 1.0
        assert pack.per_item_stats[0]["final_score"] > 0.8

    def test_real_step_closure_source_is_downweighted(self, monkeypatch):
        monkeypatch.setenv("PP_SOURCE_FILTER", "1")
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        engine = _engine_with_result(
            monkeypatch,
            ("step", 1.0, "useful enough content", "vector"),
            {"source": "step-closure", "memory_type": "reflection"},
        )

        pack = engine._supply_python("query", [0.0], debug=True)

        assert pack.per_item_stats[0]["source_penalty"] == 0.3

    def test_step_auditor_source_is_downweighted(self, monkeypatch):
        monkeypatch.setenv("PP_SOURCE_FILTER", "1")
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        engine = _engine_with_result(
            monkeypatch,
            ("audit", 1.0, "useful enough content", "vector"),
            {"source": "step_auditor", "memory_type": "reflection"},
        )

        pack = engine._supply_python("query", [0.0], debug=True)

        assert pack.per_item_stats[0]["source_penalty"] == 0.3

    def test_context_item_uses_memory_source_not_retrieval_channel(self, monkeypatch):
        monkeypatch.setenv("PP_SOURCE_FILTER", "1")
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        engine = _engine_with_result(
            monkeypatch,
            ("daemon", 1.0, "useful enough content", "fts"),
            {"source": "maintenance_daemon", "memory_type": "reflection"},
        )

        pack = engine._supply_python("query", [0.0], debug=True)

        recalled = pack.core + pack.related + pack.divergent
        assert recalled[0].source == "maintenance_daemon"
        assert pack.per_item_stats[0]["retrieval_source"] == "fts"


class TestDebugOutput:
    def test_debug_true_returns_pipeline_stats(self, monkeypatch):
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        engine = _engine_with_result(
            monkeypatch,
            ("m1", 1.0, "useful enough content", "vector"),
            {"source": "user", "memory_type": "experience", "worth_success": 0, "worth_failure": 0},
            vector_results=[("m1", 1.0, "useful enough content", "vector")],
        )

        pack = engine._supply_python("query", [0.1], debug=True)

        assert pack.pipeline_stats["vector_count"] == 1
        assert pack.pipeline_stats["core_count"] == 1
        assert len(pack.per_item_stats) == 1

    def test_debug_false_does_not_return_extra_fields(self, monkeypatch):
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        engine = _engine_with_result(
            monkeypatch,
            ("m1", 1.0, "useful enough content", "vector"),
            {"source": "user", "memory_type": "experience", "worth_success": 0, "worth_failure": 0},
        )

        pack = engine._supply_python("query", [0.0], debug=False)

        assert pack.pipeline_stats == {}
        assert pack.per_item_stats == []

    def test_per_item_stats_have_correct_shape(self, monkeypatch):
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        engine = _engine_with_result(
            monkeypatch,
            ("m1", 1.0, "useful enough content", "vector"),
            {
                "source": "user",
                "memory_type": "experience",
                "tier": "L1",
                "category": "fact",
                "worth_success": 2,
                "worth_failure": 0,
            },
            vector_results=[("m1", 1.0, "useful enough content", "vector")],
        )

        pack = engine._supply_python("query", [0.1], debug=True)
        stats = pack.per_item_stats[0]

        assert {
            "id",
            "content",
            "vector_score",
            "bm25_score",
            "fts_score",
            "graph_score",
            "fused_score",
            "worth",
            "decay_multiplier",
            "length_norm_factor",
            "source_penalty",
            "final_score",
            "source",
            "memory_type",
            "tier",
            "category",
            "layer",
        }.issubset(stats.keys())
        assert stats["source"] == "user"
        assert stats["memory_type"] == "experience"
        assert stats["tier"] == "L1"
        assert stats["category"] == "fact"


class TestRecallCache:
    def test_cache_key_includes_strict_flag(self):
        loose = memory_tools._cache_key("q", "general", 20, "global", debug=False, strict=False)
        strict = memory_tools._cache_key("q", "general", 20, "global", debug=False, strict=True)

        assert loose != strict


class TestSmartExtractorEfficiency:
    def test_generate_l0_l1_splits_once(self, monkeypatch):
        calls = {"count": 0}

        def fake_split(text):
            calls["count"] += 1
            return ["first sentence"]

        monkeypatch.setattr(smart_extractor, "_split_memory_sentences", fake_split)

        l0, _l1 = smart_extractor._generate_l0_l1("first sentence. second sentence", "fact")

        assert calls["count"] == 1
        assert l0 == "first sentence"
