import pytest

from plastic_promise.core.embedder import CachedEmbedder, OllamaEmbedder
from plastic_promise.core.memory_index import build_index_material, effective_embedding_model_name
from plastic_promise.memory.pipeline import MemoryPipeline


class _FakeEmbeddingResponse:
    def __init__(self, vector):
        self._vector = vector

    def raise_for_status(self):
        return None

    def json(self):
        return {"embedding": self._vector}


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
