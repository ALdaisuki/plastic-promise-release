import pytest

from plastic_promise.core.embedder import OllamaEmbedder


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
