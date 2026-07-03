"""Tests for 7-unit vertical slice — query_expander, decay_ranking, reranker, MMR."""


class TestQueryExpander:
    def test_chinese_substring_match(self):
        from plastic_promise.core.query_expander import expand_query

        result = expand_query("挂了")  # 挂了
        assert "crash" in result or "error" in result

    def test_english_word_boundary(self):
        from plastic_promise.core.query_expander import expand_query

        result = expand_query("I forgot the config")
        assert len(result) > len("I forgot the config")

    def test_no_match_returns_original(self):
        from plastic_promise.core.query_expander import expand_query

        assert expand_query("xyzzy unknown term") == "xyzzy unknown term"

    def test_domain_filter(self):
        from plastic_promise.core.query_expander import expand_query

        result = expand_query("挂了", "fixing")
        assert "crash" in result

    def test_empty_query(self):
        from plastic_promise.core.query_expander import expand_query

        assert expand_query("") == ""
        assert expand_query("x") == "x"


class TestDecayRanking:
    def test_method_exists(self):
        from plastic_promise.core.context_engine import ContextEngine

        assert hasattr(ContextEngine, "_apply_decay_awareness")

    def test_no_mem_returns_score_unchanged(self):
        from plastic_promise.core.context_engine import ContextEngine

        result = ContextEngine._apply_decay_awareness(0.8, None, "", 1.0)
        assert result == 0.8

    def test_fresh_memory_gets_recency_boost(self):
        import datetime

        from plastic_promise.core.context_engine import ContextEngine

        now = datetime.datetime.now()
        fresh = (now - datetime.timedelta(hours=1)).isoformat()
        mem = {"created_at": fresh, "effective_half_life": 60.0}
        result = ContextEngine._apply_decay_awareness(0.7, mem, now.isoformat(), 1.0)
        assert result > 0.7  # boost applied for 1-hour-old memory

    def test_old_memory_gets_penalized(self):
        import datetime

        from plastic_promise.core.context_engine import ContextEngine

        now = datetime.datetime.now().isoformat()
        old = (datetime.datetime.now() - datetime.timedelta(days=365)).isoformat()
        mem = {"created_at": old, "effective_half_life": 60.0}
        result = ContextEngine._apply_decay_awareness(0.8, mem, now, 1.0)
        assert result <= 0.8  # penalized

    def test_env_gate_disables(self, monkeypatch):
        monkeypatch.setenv("PP_DECAY_IN_RANKING", "0")
        from plastic_promise.core.context_engine import ContextEngine

        result = ContextEngine._apply_decay_awareness(0.5, {"created_at": "2020-01-01"}, "", 1.0)
        assert result == 0.5  # unchanged when gate is off


class TestReranker:
    def test_multiprovider_imports(self):
        from plastic_promise.core.reranker import MultiProviderReranker

        r = MultiProviderReranker()
        assert "cosine" in r._providers

    def test_disabled_returns_unchanged(self, monkeypatch):
        monkeypatch.setenv("PP_RERANK_DISABLED", "1")
        from plastic_promise.core.reranker import MultiProviderReranker

        r = MultiProviderReranker()

        # Create a mock ContextItem-like object
        class MockItem:
            def __init__(self, id, content, relevance):
                self.id = id
                self.content = content
                self.relevance = relevance
                self.is_principle = False

        items = [MockItem("a", "test", 0.8), MockItem("b", "test2", 0.6)]
        result = r.rerank("query", items)
        assert result == items  # unchanged

    def test_backward_compat_shim(self, monkeypatch):
        monkeypatch.setenv("PP_RERANK_PROVIDERS", "cosine")
        from plastic_promise.core.reranker import cross_encode_rerank

        result = cross_encode_rerank(
            "test tuple shim",
            [("id1", "content a", 0.9), ("id2", "content b", 0.5)],
        )
        assert len(result) == 2

    def test_cosine_fallback_preserves_order(self):
        from plastic_promise.core.reranker import MultiProviderReranker

        mp = MultiProviderReranker()
        scores = mp._rerank_cosine("q", [], 999)
        assert scores == {}

        # With items
        class MockItem:
            def __init__(self, id, content, relevance):
                self.id = id
                self.content = content
                self.relevance = relevance

        items = [MockItem("a", "first", 0.9), MockItem("b", "second", 0.3)]
        scores = mp._rerank_cosine("q", items, 999)
        assert scores[0] > scores[1]  # first item gets higher score

    def test_jina_auth_header_is_optional(self, monkeypatch):
        import json
        import urllib.request

        from plastic_promise.core.reranker import MultiProviderReranker

        seen = {}

        class FakeResponse:
            def read(self):
                return json.dumps({"results": [{"index": 0, "relevance_score": 0.9}]}).encode()

        def fake_urlopen(req, timeout):
            seen["auth"] = req.get_header("Authorization")
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.delenv("JINA_API_KEY", raising=False)
        MultiProviderReranker()._rerank_jina("q", [("id", "content", 0.5)], 9999999999)
        assert seen["auth"] is None

        monkeypatch.setenv("JINA_API_KEY", "test-key")
        MultiProviderReranker()._rerank_jina("q", [("id", "content", 0.5)], 9999999999)
        assert seen["auth"] == "Bearer test-key"

    def test_siliconflow_auth_header_is_optional(self, monkeypatch):
        import json
        import urllib.request

        from plastic_promise.core.reranker import MultiProviderReranker

        seen = {}

        class FakeResponse:
            def read(self):
                return json.dumps({"results": [{"index": 0, "relevance_score": 0.9}]}).encode()

        def fake_urlopen(req, timeout):
            seen["auth"] = req.get_header("Authorization")
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        MultiProviderReranker()._rerank_siliconflow("q", [("id", "content", 0.5)], 9999999999)
        assert seen["auth"] is None

        monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")
        MultiProviderReranker()._rerank_siliconflow("q", [("id", "content", 0.5)], 9999999999)
        assert seen["auth"] == "Bearer test-key"


class TestMMRFix:
    def test_get_vector_method_exists(self):
        from plastic_promise.core.lancedb_store import LanceDBStore

        assert "get_vector" in dir(LanceDBStore)


class TestNoiseFilter:
    def test_emoji_detection(self):
        from plastic_promise.core.noise_filter import is_noise

        assert is_noise("\U0001f44d")  # thumbs up emoji
        assert not is_noise("fix the bug")
        assert is_noise("")
