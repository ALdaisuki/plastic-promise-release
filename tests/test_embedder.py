import pytest

from plastic_promise.core.embedder import CachedEmbedder, OllamaEmbedder
from plastic_promise.core.memory_index import (
    SUMMARY_POLICY,
    build_index_material,
    effective_embedding_model_name,
    index_metadata,
    prepare_index_material,
)
from plastic_promise.core.semantic_chunk_enrichment import decode_embedding_plan, is_embedding_plan
from plastic_promise.memory.pipeline import MemoryPipeline

TEST_DIGEST = "sha256:" + "a" * 64
RAW_TEST_DIGEST = "a" * 64
DIGEST_ONE = "sha256:" + "b" * 64
DIGEST_TWO = "sha256:" + "c" * 64


class _FakeEmbeddingResponse:
    def __init__(self, vector):
        self._vector = vector

    def raise_for_status(self):
        return None

    def json(self):
        return {"embedding": self._vector}


class _FakeChatResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        import json

        return {"message": {"content": json.dumps(self._payload)}}


class _FakeTagsResponse:
    def __init__(self, digest=RAW_TEST_DIGEST):
        self._digest = digest

    def raise_for_status(self):
        return None

    def json(self):
        return {"models": [{"name": "qwen3:8b", "digest": self._digest}]}


def _mock_enrichment_digest(monkeypatch, digest=TEST_DIGEST):
    monkeypatch.setattr(
        "plastic_promise.core.semantic_chunk_enrichment.requests.get",
        lambda *args, **kwargs: _FakeTagsResponse(digest.removeprefix("sha256:")),
    )


def test_ollama_embedder_short_text_uses_single_request(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json["prompt"])
        return _FakeEmbeddingResponse([3.0, 4.0])

    monkeypatch.setattr("plastic_promise.core.embedder.requests.post", fake_post)
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "4")

    vec = OllamaEmbedder(host="http://127.0.0.1:11434").embed("abc")

    assert calls == ["abc"]
    assert vec == [3.0, 4.0]


def test_ollama_embedder_long_text_chunks_and_pools(monkeypatch):
    calls = []
    vectors = {
        "aaaa": [1.0, 0.0],
        "bbbb": [0.0, 1.0],
    }

    def fake_post(url, json, timeout):
        prompt = json["prompt"]
        calls.append(prompt)
        return _FakeEmbeddingResponse(vectors[prompt])

    monkeypatch.setattr("plastic_promise.core.embedder.requests.post", fake_post)
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "4")
    monkeypatch.setenv("EMBEDDER_MAX_CHUNKS", "2")

    vec = OllamaEmbedder(host="http://127.0.0.1:11434").embed("aaaabbbbcccc")

    assert calls == ["aaaa", "bbbb"]
    assert vec == pytest.approx([0.70710678, 0.70710678])


def test_ollama_embedder_shadow_keeps_legacy_requests_and_reports_candidate(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json["prompt"])
        return _FakeEmbeddingResponse([1.0, 0.0])

    monkeypatch.setattr("plastic_promise.core.embedder.requests.post", fake_post)
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "shadow")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "24")
    monkeypatch.setenv("EMBEDDER_MAX_CHUNKS", "2")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "48")

    text = "# Retrieval\n\n" + ("First topic. " * 8) + "\n\nTAIL-EVIDENCE"
    embedder = OllamaEmbedder(host="http://127.0.0.1:11434")
    vec = embedder.embed(text)

    assert vec == [1.0, 0.0]
    diagnostics = embedder.last_chunking_diagnostics
    assert diagnostics["mode"] == "shadow"
    assert diagnostics["active_mode"] == "legacy"
    assert diagnostics["legacy"]["chunk_count"] == 2
    assert diagnostics["legacy"]["truncated"] is True
    assert diagnostics["candidate"]["chunk_count"] > diagnostics["legacy"]["chunk_count"]
    assert diagnostics["candidate"]["last_source_end"] == len(text)
    assert diagnostics["candidate"]["truncated"] is False
    assert "paragraph" in diagnostics["candidate"]["kinds"]
    assert all("TAIL-EVIDENCE" not in prompt for prompt in calls)


def test_ollama_embedder_shadow_matches_legacy_embedding_identity(monkeypatch):
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "4")
    monkeypatch.setenv("EMBEDDER_MAX_CHUNKS", "2")
    text = "aaaabbbbcccc"
    calls_by_mode = {}

    def fake_post(url, json, timeout):
        calls_by_mode.setdefault(current_mode[0], []).append(json["prompt"])
        return _FakeEmbeddingResponse([1.0, 0.0] if json["prompt"] == "aaaa" else [0.0, 1.0])

    monkeypatch.setattr("plastic_promise.core.embedder.requests.post", fake_post)
    current_mode = ["off"]
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "off")
    legacy = OllamaEmbedder(host="http://127.0.0.1:11434")
    legacy_vec = legacy.embed(text)
    current_mode[0] = "shadow"
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "shadow")
    shadow = OllamaEmbedder(host="http://127.0.0.1:11434")
    shadow_vec = shadow.embed(text)

    assert calls_by_mode["shadow"] == calls_by_mode["off"] == ["aaaa", "bbbb"]
    assert shadow_vec == pytest.approx(legacy_vec)
    assert shadow.index_model_name == legacy.index_model_name == "mxbai-embed-large"


def test_ollama_embedder_structure_v1_preserves_tail_and_reports_diagnostics(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json["prompt"])
        return _FakeEmbeddingResponse([1.0, 0.0])

    monkeypatch.setattr("plastic_promise.core.embedder.requests.post", fake_post)
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "24")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "48")

    text = "# Retrieval\n\n" + ("First topic. " * 8) + "\n\nTAIL-EVIDENCE"
    embedder = OllamaEmbedder(host="http://127.0.0.1:11434")
    vec = embedder.embed(text)

    assert vec == [1.0, 0.0]
    assert any("TAIL-EVIDENCE" in prompt for prompt in calls)
    assert embedder.last_chunking_diagnostics["mode"] == "structure-v1"
    assert embedder.last_chunking_diagnostics["last_source_end"] == len(text)
    assert embedder.last_chunking_diagnostics["truncated"] is False


def test_shadow_configuration_keeps_legacy_index_material(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "shadow")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "256")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "512")
    embedder = CachedEmbedder(OllamaEmbedder(host="http://127.0.0.1:11434"))

    model_name = effective_embedding_model_name(embedder)
    shadow = build_index_material({"content": "same text"}, model_name=model_name)
    legacy = build_index_material({"content": "same text"}, model_name="mxbai-embed-large")

    assert model_name == "mxbai-embed-large"
    assert shadow.embedding_hash == legacy.embedding_hash


def test_cached_embedder_exposes_shadow_diagnostics(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "shadow")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "4")
    monkeypatch.setenv("EMBEDDER_MAX_CHUNKS", "2")
    monkeypatch.setattr(
        "plastic_promise.core.embedder.requests.post",
        lambda url, json, timeout: _FakeEmbeddingResponse([1.0, 0.0]),
    )

    embedder = CachedEmbedder(OllamaEmbedder(host="http://127.0.0.1:11434"))
    embedder.embed("abcdefghijk")

    assert embedder.last_chunking_diagnostics["mode"] == "shadow"


def test_structure_v1_configuration_is_bound_to_index_material(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "256")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "512")
    embedder = CachedEmbedder(OllamaEmbedder(host="http://127.0.0.1:11434"))

    model_name = effective_embedding_model_name(embedder)
    structured = build_index_material({"content": "same text"}, model_name=model_name)
    legacy = build_index_material({"content": "same text"}, model_name="mxbai-embed-large")

    assert "chunking=structure-v1" in model_name
    assert "target_chars=256" in model_name
    assert "max_chunks=64" in model_name
    assert structured.embedding_hash != legacy.embedding_hash


def test_structure_v1_source_guard_is_terminal_and_diagnostic(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS", "4")

    embedder = OllamaEmbedder(host="http://127.0.0.1:11434")

    with pytest.raises(ValueError, match="structure_chunking_source_too_large"):
        embedder.embed("12345")

    assert embedder.last_chunking_diagnostics == {
        "mode": "structure-v1",
        "source_chars": 5,
        "resource_limited": True,
        "error": "structure_chunking_source_too_large",
    }


def test_shadow_source_guard_skips_candidate_planning_but_keeps_legacy_embedding(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json["prompt"])
        return _FakeEmbeddingResponse([1.0, 0.0])

    monkeypatch.setattr("plastic_promise.core.embedder.requests.post", fake_post)
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "shadow")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "4")
    monkeypatch.setenv("EMBEDDER_MAX_CHUNKS", "2")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS", "4")

    embedder = OllamaEmbedder(host="http://127.0.0.1:11434")
    assert embedder.embed("abcdefghijk") == [1.0, 0.0]
    assert calls == ["abcd", "efgh"]
    assert embedder.last_chunking_diagnostics["candidate"]["error"] == (
        "structure_chunking_source_too_large"
    )


def test_memory_pipeline_uses_chunking_aware_index_identity(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "256")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "512")

    embedder = OllamaEmbedder(host="http://127.0.0.1:11434")

    assert MemoryPipeline._embedding_model_name(embedder) == embedder.index_model_name


def test_structure_enrichment_on_changes_only_derived_embedding_input(monkeypatch, tmp_path):
    embedding_prompts = []
    source = "# Retrieval\nThe API timeout is 30 seconds for request_id req-17.\n"

    def fake_post(url, json, timeout):
        if url.endswith("/api/chat"):
            return _FakeChatResponse(
                {
                    "summary": "The API timeout is 30 seconds for request_id req-17.",
                    "keywords": ["API timeout", "request_id"],
                    "entities": ["req-17"],
                    "identifiers": ["30", "request_id", "req-17"],
                    "evidence": ["The API timeout is 30 seconds for request_id req-17."],
                }
            )
        embedding_prompts.append(json["prompt"])
        return _FakeEmbeddingResponse([1.0, 0.0])

    monkeypatch.setattr("plastic_promise.core.embedder.requests.post", fake_post)
    monkeypatch.setattr("plastic_promise.core.semantic_chunk_enrichment.requests.post", fake_post)
    _mock_enrichment_digest(monkeypatch)
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT", "on")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST", TEST_DIGEST)
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_CACHE_PATH", str(tmp_path / "enrichment.db"))
    embedder = OllamaEmbedder(host="http://127.0.0.1:11434")

    material = prepare_index_material({"content": source}, embedder=embedder, policy="legacy")
    embedder.embed(material.vector_text)

    assert is_embedding_plan(material.vector_text)
    assert embedding_prompts[0].startswith("[Semantic context]")
    assert embedding_prompts[0].endswith(
        "Retrieval\nThe API timeout is 30 seconds for request_id req-17."
    )
    assert embedder.last_index_preparation_diagnostics["enriched"] == 1
    assert embedder.last_chunking_diagnostics["mode"] == "embedding-plan-v1"
    assert index_metadata(material)["embedding_plan"]["enriched_count"] == 1
    assert "enrichment=semantic-v1" in embedder.index_model_name
    assert f"enrichment_model=qwen3:8b@{TEST_DIGEST}" in embedder.index_model_name


def test_structure_enrichment_shadow_preserves_vector_input_and_identity(monkeypatch, tmp_path):
    embedding_prompts = []

    def fake_post(url, json, timeout):
        if url.endswith("/api/chat"):
            return _FakeChatResponse(
                {
                    "summary": "The API timeout is 30 seconds for request_id req-17.",
                    "keywords": ["API timeout", "request_id"],
                    "entities": ["req-17"],
                    "identifiers": ["30", "request_id", "req-17"],
                    "evidence": ["The API timeout is 30 seconds for request_id req-17."],
                }
            )
        embedding_prompts.append(json["prompt"])
        return _FakeEmbeddingResponse([1.0, 0.0])

    monkeypatch.setattr("plastic_promise.core.embedder.requests.post", fake_post)
    _mock_enrichment_digest(monkeypatch)
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT", "shadow")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST", TEST_DIGEST)
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_CACHE_PATH", str(tmp_path / "enrichment.db"))
    embedder = OllamaEmbedder(host="http://127.0.0.1:11434")
    identity = embedder.index_model_name

    source = "# Retrieval\nThe API timeout is 30 seconds for request_id req-17.\n"
    prepared = embedder.prepare_index_text(source)
    embedder.embed(source)

    assert prepared == source
    assert embedding_prompts == ["Retrieval\nThe API timeout is 30 seconds for request_id req-17."]
    assert "enrichment=" not in identity
    assert embedder.last_index_preparation_diagnostics["queued"] == 1


def test_enrichment_requires_structure_v1(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "off")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT", "on")

    embedder = OllamaEmbedder(host="http://127.0.0.1:11434")

    assert embedder.index_model_name == "mxbai-embed-large"


def test_active_enrichment_never_calls_chat_for_query_embedding(monkeypatch, tmp_path):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(url)
        if url.endswith("/api/chat"):
            raise AssertionError("query embedding must not call semantic enrichment")
        return _FakeEmbeddingResponse([1.0, 0.0])

    monkeypatch.setattr("plastic_promise.core.embedder.requests.post", fake_post)
    _mock_enrichment_digest(monkeypatch)
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT", "on")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST", TEST_DIGEST)
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_CACHE_PATH", str(tmp_path / "enrichment.db"))
    embedder = OllamaEmbedder(host="http://127.0.0.1:11434")

    assert embedder.embed("find request_id req-17") == [1.0, 0.0]
    assert calls == ["http://127.0.0.1:11434/api/embeddings"]


def test_fallback_and_later_success_have_distinct_persisted_material_hashes(monkeypatch, tmp_path):
    payloads = [
        {
            "summary": "not in source",
            "keywords": [],
            "entities": [],
            "identifiers": ["30", "request_id", "req-17"],
            "evidence": ["not in source"],
        },
        {
            "summary": "The API timeout is 30 seconds for request_id req-17.",
            "keywords": ["API timeout", "request_id"],
            "entities": ["req-17"],
            "identifiers": ["30", "request_id", "req-17"],
            "evidence": ["The API timeout is 30 seconds for request_id req-17."],
        },
    ]

    monkeypatch.setattr(
        "plastic_promise.core.semantic_chunk_enrichment.requests.post",
        lambda *args, **kwargs: _FakeChatResponse(payloads.pop(0)),
    )
    _mock_enrichment_digest(monkeypatch)
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT", "on")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST", TEST_DIGEST)
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_CACHE_PATH", str(tmp_path / "enrichment.db"))
    embedder = OllamaEmbedder(host="http://127.0.0.1:11434")
    record = {"content": "# Retrieval\nThe API timeout is 30 seconds for request_id req-17.\n"}

    fallback = prepare_index_material(record, embedder=embedder, policy="legacy")
    enriched = prepare_index_material(record, embedder=embedder, policy="legacy")

    assert fallback.embedding_hash != enriched.embedding_hash
    assert index_metadata(fallback)["embedding_plan"]["fallback_count"] == 1
    assert index_metadata(enriched)["embedding_plan"]["enriched_count"] == 1


def test_summary_policy_model_migration_rebuilds_from_canonical_summary(monkeypatch, tmp_path):
    payload = {
        "summary": "Grounded retrieval metadata.",
        "keywords": ["retrieval"],
        "entities": [],
        "identifiers": [],
        "evidence": ["Grounded retrieval metadata."],
    }
    monkeypatch.setattr(
        "plastic_promise.core.semantic_chunk_enrichment.requests.post",
        lambda *args, **kwargs: _FakeChatResponse(payload),
    )
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT", "on")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_CACHE_PATH", str(tmp_path / "enrichment.db"))
    record = {
        "content": "Grounded retrieval metadata.",
        "l0_abstract": "Grounded retrieval metadata.",
        "l1_summary": "- Grounded retrieval metadata.",
        "embedding_text": ("L0: Grounded retrieval metadata.\nL1: - Grounded retrieval metadata."),
        "search_text": "Grounded retrieval metadata.",
    }

    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST", DIGEST_ONE)
    _mock_enrichment_digest(monkeypatch, DIGEST_ONE)
    first_embedder = OllamaEmbedder(host="http://127.0.0.1:11434")
    first = prepare_index_material(record, embedder=first_embedder, policy=SUMMARY_POLICY)

    persisted = dict(record)
    persisted["embedding_text"] = first.vector_text
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST", DIGEST_TWO)
    _mock_enrichment_digest(monkeypatch, DIGEST_TWO)
    second_embedder = OllamaEmbedder(host="http://127.0.0.1:11434")
    second = prepare_index_material(persisted, embedder=second_embedder, policy=SUMMARY_POLICY)
    plan = decode_embedding_plan(second.vector_text)

    assert plan["model_identity"] == f"qwen3:8b@{DIGEST_TWO}"
    assert all(not is_embedding_plan(chunk["source_text"]) for chunk in plan["chunks"])
