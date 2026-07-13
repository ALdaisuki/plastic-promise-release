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
    engine._text_retrieval = lambda query, trust_boost=1.0, domain_hint=None: []
    engine._vector_retrieval = lambda vector, scope=None: vector_results or []
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
        engine._memories["m1"] = {
            "id": "m1",
            "content": "exact text",
            "memory_type": "experience",
            "source": "test",
        }

        assert engine._fts_retrieval("exact") == [("m1", 0.9, "exact text", "fts")]

    def test_fts_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("PP_FTS_DISABLED", "1")

        class FakeLDB:
            def fts_search(self, query, k=20, scope=None):
                raise AssertionError("FTS should be skipped")

        engine = ContextEngine(use_sqlite=False)
        engine._ldb = FakeLDB()
        engine._memories["m1"] = {
            "id": "m1",
            "content": "exact text",
            "memory_type": "experience",
            "source": "test",
        }

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
        engine._memories["m1"] = {
            "id": "m1",
            "content": "exact text",
            "memory_type": "experience",
            "source": "test",
            "domain": "designing",
        }

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
            def select(self, _columns):
                return self

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
            def select(self, _columns):
                return self

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

    def test_prefixed_daemon_audit_telemetry_is_excluded(self, monkeypatch):
        monkeypatch.setenv("PP_SOURCE_FILTER", "1")
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        engine = _engine_with_result(
            monkeypatch,
            (
                "daemon_audit",
                1.0,
                "[maintenance_daemon] AUDIT trust=0.60 pipeline=0.00 domain=0.80 bridge=1.00 mem_q=0.60 -> 0.60 | fixes: recovered stuck tasks",
                "fts",
            ),
            {"source": "maintenance_daemon", "memory_type": "reflection"},
        )

        pack = engine._supply_python("maintenance daemon audit", [0.0], debug=True)

        assert pack.total_items == 0
        assert pack.pipeline_stats["after_noise_filter"] == 0


class TestRequestScope:
    def test_request_scope_generates_defaults(self):
        from plastic_promise.mcp.tools.request_scope import build_request_scope

        scope = build_request_scope({}, "memory_recall")

        assert scope["stage_session_id"] == "session:memory_recall:default"
        assert scope["flow_line_id"] == "default"
        assert scope["request_id"].startswith("req:")
        assert scope["request_scope_id"].startswith(
            "session:memory_recall:default::flow:default::req:"
        )

    def test_request_scope_preserves_caller_ids(self):
        from plastic_promise.mcp.tools.request_scope import build_request_scope

        scope = build_request_scope(
            {
                "stage_session_id": "stage:codex:test",
                "flow_line_id": "normal-development",
                "request_id": "req:fixed",
            },
            "context_supply",
        )

        assert scope == {
            "stage_session_id": "stage:codex:test",
            "flow_line_id": "normal-development",
            "request_id": "req:fixed",
            "request_scope_id": "stage:codex:test::flow:normal-development::req:req:fixed",
        }

    def test_memory_recall_cache_key_includes_request_scope(self):
        from plastic_promise.mcp.tools.memory import _cache_key

        key_a = _cache_key(
            "same query",
            "debugging",
            5,
            "global",
            request_scope_id="stage:a::flow:one::req:1",
        )
        key_b = _cache_key(
            "same query",
            "debugging",
            5,
            "global",
            request_scope_id="stage:a::flow:two::req:1",
        )

        assert key_a != key_b

    def test_context_supply_adds_request_scope_to_audit_metadata(self, monkeypatch):
        import asyncio

        import plastic_promise.core.embedder as embedder_mod
        from plastic_promise.mcp.tools.context import handle_context_supply

        class FakeEmbedder:
            async def aembed(self, text):
                return [0.0]

        class FakePack:
            def __init__(self):
                self.audit_metadata = {}

            def to_prompt(self):
                return str(self.audit_metadata)

        class FakeEngine:
            def supply(self, task_description, task_vector, task_type, scope):
                return FakePack()

        monkeypatch.setattr(embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder())

        async def run():
            result = await handle_context_supply(
                FakeEngine(),
                {
                    "task_description": "debug recall",
                    "stage_session_id": "stage:x",
                    "flow_line_id": "flow:y",
                    "request_id": "req:z",
                },
            )
            return result[0].text

        text = asyncio.run(run())
        assert "stage:x::flow:flow:y::req:req:z" in text

    def test_context_pack_prompt_renders_request_scope_for_real_pack(self):
        from plastic_promise.core.context_engine import ContextPack

        pack = ContextPack()
        pack.audit_metadata["request_scope"] = {
            "stage_session_id": "stage:codex:visible",
            "flow_line_id": "audit-review",
            "request_id": "req:visible",
            "request_scope_id": "stage:codex:visible::flow:audit-review::req:req:visible",
        }

        text = pack.to_prompt()

        assert "## [REQUEST_SCOPE] Audit Trace" in text
        assert "stage:codex:visible::flow:audit-review::req:req:visible" in text

    def test_supply_python_does_not_mutate_shared_domain_hint(self, monkeypatch):
        engine = ContextEngine(use_sqlite=False)
        engine._domain_hint = "existing"

        captured = {}

        def fake_text(task, trust_boost=1.0, domain_hint=None):
            captured["domain_hint"] = domain_hint
            return []

        monkeypatch.setattr(engine, "_ensure_heavy_init", lambda: None)
        monkeypatch.setattr(engine, "_activate_principles", lambda task_type, task: [])
        monkeypatch.setattr(engine, "_inject_activated_to_graph", lambda activated, task_type: 0)
        monkeypatch.setattr(engine, "_graph_traversal", lambda task_type: [])
        monkeypatch.setattr(engine, "_text_retrieval", fake_text)
        monkeypatch.setattr(engine, "_vector_retrieval", lambda task_vector, scope=None: [])
        monkeypatch.setattr(engine, "_fts_retrieval", lambda query, scope: [])
        monkeypatch.setattr(engine, "_apply_edge_feedback", lambda: None)

        engine._supply_python("task", [0.0], "general", "building")

        assert captured["domain_hint"] == "building"
        assert engine._domain_hint == "existing"


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


class TestQueryExpansionIntegration:
    def test_supply_python_expands_query_without_os_env_crash(self, monkeypatch):
        monkeypatch.setenv("PP_QUERY_EXPANSION", "1")
        monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
        monkeypatch.setenv("PP_RERANK_DISABLED", "1")
        monkeypatch.setenv("PP_CODE_MEMORY_ENABLED", "0")
        seen = {}
        engine = ContextEngine(use_sqlite=False)
        engine._ensure_heavy_init = lambda: None
        engine._activate_principles = lambda task_type, task_description: []
        engine._inject_activated_to_graph = lambda activated, task_type: 0
        engine._graph_traversal = lambda task_type: []
        engine._vector_retrieval = lambda vector, scope=None: []
        engine._fts_retrieval = lambda query, scope="global": []
        engine._apply_edge_feedback = lambda: None
        engine._apply_decay_awareness = lambda score, mem, current_time, trust_boost: score
        engine._apply_mmr = lambda items, threshold=0.85, penalty=0.70: items
        engine._compute_divergent_quality = lambda items, all_items: items
        engine._calc_freshness = lambda item_id: "valid"
        engine._calc_decay_status = lambda item_id, mem: "healthy"
        engine._memories = {
            "m1": {"source": "user", "memory_type": "experience", "worth_success": 0, "worth_failure": 0}
        }

        def text_retrieval(query, trust_boost=1.0, domain_hint=None):
            seen["query"] = query
            return [("m1", 0.9, "useful enough memory retrieval content", "bm25")]

        engine._text_retrieval = text_retrieval

        pack = engine._supply_python("I forgot the config", [0.0], "debugging", "fixing", debug=True)

        assert pack.total_items == 1
        assert seen["query"] != "I forgot the config"
        assert any(term in seen["query"] for term in ("memory", "recall", "记忆"))

    def test_memory_recall_handler_does_not_return_os_env_error(self, monkeypatch):
        import asyncio
        import json

        import plastic_promise.adaptive_retrieval as adaptive_retrieval
        import plastic_promise.core.embedder as embedder_mod
        from plastic_promise.core.context_engine import ContextPack
        from plastic_promise.mcp.tools.memory import handle_memory_recall

        class FakeEmbedder:
            async def aembed(self, text):
                return [0.0]

        class FakeEngine:
            def supply(self, query, vec, task_type, scope, debug=False):
                return ContextPack()

        monkeypatch.setattr(adaptive_retrieval, "should_retrieve", lambda query: True)
        monkeypatch.setattr(embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder())

        result = asyncio.run(
            handle_memory_recall(FakeEngine(), {"query": "remember config", "debug": True})
        )
        payload = json.loads(result[0].text)

        assert "error" not in payload
        assert "_os_env" not in result[0].text

    def test_context_supply_handler_does_not_return_os_env_error(self, monkeypatch):
        import asyncio
        import json

        import plastic_promise.core.embedder as embedder_mod
        from plastic_promise.core.context_engine import ContextPack
        from plastic_promise.mcp.tools.context import handle_context_supply

        class FakeEmbedder:
            async def aembed(self, text):
                return [0.0]

        class FakeEngine:
            def supply(self, task_description, task_vector, task_type, scope):
                return ContextPack()

        monkeypatch.setattr(embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder())

        result = asyncio.run(
            handle_context_supply(FakeEngine(), {"task_description": "debug config recall"})
        )

        assert "_os_env" not in result[0].text
        try:
            payload = json.loads(result[0].text)
        except json.JSONDecodeError:
            payload = {}
        assert "error" not in payload


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
