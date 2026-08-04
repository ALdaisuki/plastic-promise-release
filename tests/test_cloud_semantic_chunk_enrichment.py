from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from plastic_promise.core.chunking import ChunkMaterial
from plastic_promise.core.semantic_chunk_enrichment import (
    SYSTEM_PROMPT,
    SemanticChunkEnricher,
    decode_embedding_plan,
    is_embedding_plan,
)

SOURCE = "# Retrieval\nThe API timeout is 30 seconds for request_id req-17.\n"
FRAGMENT = "The API timeout is 30 seconds for request_id req-17."


class _Inference:
    def __init__(self, response=None, error=None):
        self.response = response or {
            "summary": FRAGMENT,
            "keywords": ["API timeout", "request_id"],
            "entities": ["req-17"],
            "identifiers": ["30", "request_id", "req-17"],
            "evidence": [FRAGMENT],
        }
        self.error = error
        self.calls = []
        self.closed = False

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


class _EchoInference(_Inference):
    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        user_payload = kwargs["user_payload"]
        fragment = user_payload["source_fragment"]
        return {
            "summary": fragment,
            "keywords": [],
            "entities": [],
            "identifiers": user_payload["required_identifiers"],
            "evidence": [fragment],
        }


class _EmbeddingHTTPClient:
    def __init__(self):
        self.calls = []
        self.closed = False

    def post_json(self, path, payload, *, deadline=None):
        self.calls.append((path, payload, deadline))
        rows = [
            {
                "index": index,
                "embedding": [1.0, 0.0] if index % 2 == 0 else [0.0, 1.0],
            }
            for index, _text in enumerate(payload["input"])
        ]
        return SimpleNamespace(
            payload={"data": rows},
            latency_ms=1.0,
        )

    def close(self):
        self.closed = True


def _material():
    return ChunkMaterial(
        text=FRAGMENT,
        kind="paragraph",
        heading_path=("Retrieval",),
        source_start=12,
        source_end=12 + len(FRAGMENT),
    )


def _enricher(tmp_path, inference, mode="on"):
    return SemanticChunkEnricher(
        provider="openai-compatible",
        model="deepseek-chat",
        model_revision="DeepSeek-V3.2",
        mode=mode,
        cache_path=tmp_path / "cloud-enrichment.db",
        inference_client=inference,
    )


def test_cloud_enrichment_uses_deepseek_json_provider_and_grounding_gate(tmp_path):
    inference = _Inference()
    enricher = _enricher(tmp_path, inference)

    batch = enricher.prepare_chunks([_material()], source_text=SOURCE)

    assert batch.embedding_texts[0].startswith("[Semantic context]\nSummary: " + FRAGMENT)
    assert batch.diagnostics["provider"] == "openai-compatible"
    assert batch.diagnostics["enriched"] == 1
    assert inference.calls[0]["user_payload"]["source_fragment"] == FRAGMENT
    assert inference.calls[0]["max_tokens"] == 768
    assert "json" in inference.calls[0]["system_prompt"]
    assert '{"summary":' in inference.calls[0]["system_prompt"]
    assert {"summary", "keywords", "entities", "identifiers", "evidence"} <= set(
        inference.calls[0]["system_prompt"].split('"')
    )
    assert enricher.model_identity == "openai-compatible:deepseek-chat@DeepSeek-V3.2"
    assert "enrichment_provider=openai-compatible" in enricher.index_identity

    enricher.close()
    assert inference.closed is True


def test_cloud_enrichment_defaults_match_deepseek_provider(monkeypatch, tmp_path):
    monkeypatch.delenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL", raising=False)
    monkeypatch.delenv("PP_MEMORY_CHUNK_ENRICHMENT_BASE_URL", raising=False)
    monkeypatch.delenv("PP_INFERENCE_MODEL", raising=False)
    monkeypatch.delenv("PP_INFERENCE_BASE_URL", raising=False)

    enricher = SemanticChunkEnricher(
        provider="openai-compatible",
        mode="off",
        cache_path=tmp_path / "defaults.db",
        inference_client=_Inference(),
    )

    assert enricher.model == "deepseek-v4-flash"
    assert enricher._cloud_base_url == "https://api.deepseek.com"
    assert "json" in SYSTEM_PROMPT


def test_cloud_enrichment_rejects_ungrounded_output(tmp_path):
    inference = _Inference(
        response={
            "summary": "A fabricated timeout is 60 seconds.",
            "keywords": ["timeout"],
            "entities": [],
            "identifiers": ["60"],
            "evidence": ["A fabricated timeout is 60 seconds."],
        }
    )
    batch = _enricher(tmp_path, inference).prepare_chunks([_material()], source_text=SOURCE)

    assert batch.embedding_texts == (FRAGMENT,)
    assert batch.diagnostics["errors"] == {"ungrounded_summary": 1}


def test_cloud_provider_error_is_redacted_and_falls_back_to_source(tmp_path, caplog):
    secret = "secret-that-must-never-be-logged"
    inference = _Inference(error=RuntimeError(secret + " " + FRAGMENT))
    with caplog.at_level(logging.DEBUG):
        batch = _enricher(tmp_path, inference).prepare_chunks([_material()], source_text=SOURCE)

    assert batch.embedding_texts == (FRAGMENT,)
    assert batch.diagnostics["errors"] == {"request_failed": 1}
    assert secret not in caplog.text
    assert FRAGMENT not in caplog.text


def test_cloud_off_mode_never_calls_provider(tmp_path):
    inference = _Inference(error=AssertionError("off mode must not call cloud"))
    batch = _enricher(tmp_path, inference, mode="off").prepare_chunks(
        [_material()], source_text=SOURCE
    )

    assert batch.embedding_texts == (FRAGMENT,)
    assert inference.calls == []


def test_custom_cloud_endpoint_never_reuses_deepseek_supplier_key(monkeypatch, tmp_path):
    monkeypatch.delenv("PP_MEMORY_CHUNK_ENRICHMENT_API_KEY", raising=False)
    monkeypatch.delenv("PP_INFERENCE_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key-must-not-leave-provider")
    enricher = SemanticChunkEnricher(
        provider="openai-compatible",
        base_url="https://inference.example.test/v1",
        mode="on",
        cache_path=tmp_path / "custom-endpoint.db",
    )

    with pytest.raises(ValueError, match="^inference_api_key_missing$"):
        enricher._cloud_inference_client()


def test_cloud_enrichment_endpoint_path_is_bound_into_index_identity(tmp_path):
    first = SemanticChunkEnricher(
        provider="openai-compatible",
        base_url="https://inference.example.test/v1",
        path="/first/completions",
        model_digest="sha256:" + ("a" * 64),
        mode="on",
        cache_path=tmp_path / "first.db",
        inference_client=_Inference(),
    )
    second = SemanticChunkEnricher(
        provider="openai-compatible",
        base_url="https://inference.example.test/v1",
        path="/second/completions",
        model_digest="sha256:" + ("a" * 64),
        mode="on",
        cache_path=tmp_path / "second.db",
        inference_client=_Inference(),
    )

    assert first.index_identity != second.index_identity
    assert "enrichment_endpoint_sha256=" in first.index_identity


def test_factory_cloud_enrichment_decodes_plan_and_embeds_each_chunk(monkeypatch, tmp_path):
    import plastic_promise.core.embedder as embedder_module
    from plastic_promise.core.memory_index import prepare_index_material

    embedder_module.reset_embedder()
    embedding_client = _EmbeddingHTTPClient()
    inference = _EchoInference()
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBEDDER_API_KEY", "not-a-real-key")
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://embedding.example.test/v1")
    monkeypatch.setenv("EMBEDDER_MODEL", "text-embedding-v4")
    monkeypatch.setenv("EMBEDDER_MODEL_REVISION", "revision-a")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    monkeypatch.setenv("EMBEDDER_DIMENSION", "2")
    monkeypatch.setenv("EMBEDDER_CACHE_SIZE", "0")
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENGINE", "python")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "48")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "64")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT", "on")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_PROVIDER", "openai-compatible")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL", "deepseek-chat")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL_REVISION", "DeepSeek-V3.2")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_PATH", "/derived-memory/chat/completions")
    monkeypatch.setenv(
        "PP_MEMORY_CHUNK_ENRICHMENT_CACHE_PATH", str(tmp_path / "factory-enrichment.db")
    )
    monkeypatch.setattr(
        embedder_module.OpenAICompatibleEmbedder,
        "_build_http_client",
        lambda _self: embedding_client,
    )
    monkeypatch.setattr(
        SemanticChunkEnricher,
        "_cloud_inference_client",
        lambda _self: inference,
    )
    source = (
        "# Retrieval\n"
        "Alpha evidence remains grounded in the first source paragraph. "
        "Alpha evidence continues with enough text for another chunk.\n\n"
        "## Verification\n"
        "Beta evidence remains grounded in the second source paragraph. "
        "Beta evidence continues with enough text for another chunk.\n"
    )

    try:
        selected = embedder_module.get_embedder(fallback_on_error=False)
        material = prepare_index_material({"content": source}, embedder=selected, policy="legacy")
        plan = decode_embedding_plan(material.vector_text)
        vector = selected.embed(material.vector_text)

        plan_chunks = plan["chunks"]
        assert isinstance(plan_chunks, list) and len(plan_chunks) > 1
        assert len(inference.calls) == len(plan_chunks)
        sent_inputs = [text for call in embedding_client.calls for text in call[1]["input"]]
        assert sent_inputs == [chunk["embedding_text"] for chunk in plan_chunks]
        assert all(not is_embedding_plan(text) for text in sent_inputs)
        assert len({call[2] for call in embedding_client.calls}) == 1
        assert vector and any(vector)
        assert "enrichment_endpoint_sha256=" in selected.index_model_name
    finally:
        embedder_module.reset_embedder()

    assert embedding_client.closed is True


def test_factory_structure_mode_keeps_enrichment_off_by_default(monkeypatch, tmp_path):
    import plastic_promise.core.embedder as embedder_module

    embedder_module.reset_embedder()
    embedding_client = _EmbeddingHTTPClient()
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBEDDER_API_KEY", "not-a-real-key")
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://embedding.example.test/v1")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    monkeypatch.setenv("EMBEDDER_DIMENSION", "2")
    monkeypatch.setenv("EMBEDDER_CACHE_SIZE", "0")
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.delenv("PP_MEMORY_CHUNK_ENRICHMENT", raising=False)
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_CACHE_PATH", str(tmp_path / "factory-off.db"))
    monkeypatch.setattr(
        embedder_module.OpenAICompatibleEmbedder,
        "_build_http_client",
        lambda _self: embedding_client,
    )
    monkeypatch.setattr(
        SemanticChunkEnricher,
        "_cloud_inference_client",
        lambda _self: pytest.fail("off mode must not construct inference client"),
    )

    try:
        selected = embedder_module.get_embedder(fallback_on_error=False)
        assert selected.prepare_index_text(SOURCE) == SOURCE
        assert "|enrichment=" not in selected.index_model_name
        assert embedding_client.calls == []
    finally:
        embedder_module.reset_embedder()
