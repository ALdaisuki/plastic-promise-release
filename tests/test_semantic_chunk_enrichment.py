import json
import time
from concurrent.futures import ThreadPoolExecutor

from plastic_promise.core.chunking import ChunkMaterial
from plastic_promise.core.semantic_chunk_enrichment import (
    SemanticChunkEnricher,
    decode_embedding_plan,
)

TEST_DIGEST = "sha256:" + "a" * 64
RAW_TEST_DIGEST = "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
CONFIGURED_DIGEST = "sha256:" + "c" * 64
ACTUAL_DIGEST = "sha256:" + "d" * 64


class _FakeChatResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": self._content}}


class _FakeTagsResponse:
    def __init__(self, digest: str = RAW_TEST_DIGEST):
        self._digest = digest

    def raise_for_status(self):
        return None

    def json(self):
        return {"models": [{"name": "qwen3:8b", "digest": self._digest}]}


def _material() -> ChunkMaterial:
    return ChunkMaterial(
        text="Retrieval\nThe API timeout is 30 seconds for request_id req-17.",
        kind="paragraph",
        heading_path=("Retrieval",),
        source_start=12,
        source_end=64,
    )


def _source() -> str:
    return "# Retrieval\nThe API timeout is 30 seconds for request_id req-17.\n"


def _valid_payload(**overrides) -> dict:
    payload = {
        "summary": "The API timeout is 30 seconds for request_id req-17.",
        "keywords": ["API timeout", "request_id"],
        "entities": ["req-17"],
        "identifiers": ["30", "request_id", "req-17"],
        "evidence": ["The API timeout is 30 seconds for request_id req-17."],
    }
    payload.update(overrides)
    return payload


def _enricher(monkeypatch, tmp_path, *, mode="on") -> SemanticChunkEnricher:
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT", mode)
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST", TEST_DIGEST)
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_CACHE_PATH", str(tmp_path / "enrichment.db"))
    monkeypatch.setattr(
        "plastic_promise.core.semantic_chunk_enrichment.requests.get",
        lambda *args, **kwargs: _FakeTagsResponse(),
    )
    return SemanticChunkEnricher(host="http://127.0.0.1:11434")


def test_valid_grounded_output_prepends_derived_metadata(monkeypatch, tmp_path):
    calls = []

    def fake_post(url, timeout, **kwargs):
        calls.append((url, kwargs["json"], timeout))
        return _FakeChatResponse(json.dumps(_valid_payload()))

    monkeypatch.setattr("plastic_promise.core.semantic_chunk_enrichment.requests.post", fake_post)
    enricher = _enricher(monkeypatch, tmp_path)

    material = _material()
    batch = enricher.prepare_chunks([material], source_text=_source())

    assert batch.embedding_texts[0].startswith("[Semantic context]\nSummary: The API timeout")
    assert batch.embedding_texts[0].endswith(material.text)
    assert batch.diagnostics["enriched"] == 1
    assert calls[0][0].endswith("/api/chat")
    assert calls[0][1]["model"] == "qwen3:8b"
    assert calls[0][1]["think"] is False
    assert calls[0][1]["format"]["additionalProperties"] is False
    assert calls[0][1]["options"]["temperature"] == 0


def test_evidence_not_present_in_source_fails_closed(monkeypatch, tmp_path):
    payload = _valid_payload(evidence=["The timeout is 60 seconds."])
    monkeypatch.setattr(
        "plastic_promise.core.semantic_chunk_enrichment.requests.post",
        lambda *args, **kwargs: _FakeChatResponse(json.dumps(payload)),
    )
    enricher = _enricher(monkeypatch, tmp_path)

    batch = enricher.prepare_chunks([_material()], source_text=_source())

    assert batch.embedding_texts == (_material().text,)
    assert batch.diagnostics["fallbacks"] == 1
    assert batch.diagnostics["errors"] == {"ungrounded_evidence": 1}


def test_identifier_mismatch_fails_closed(monkeypatch, tmp_path):
    payload = _valid_payload(identifiers=["30", "request_id", "req-99"])
    monkeypatch.setattr(
        "plastic_promise.core.semantic_chunk_enrichment.requests.post",
        lambda *args, **kwargs: _FakeChatResponse(json.dumps(payload)),
    )
    enricher = _enricher(monkeypatch, tmp_path)

    batch = enricher.prepare_chunks([_material()], source_text=_source())

    assert batch.embedding_texts == (_material().text,)
    assert batch.diagnostics["errors"] == {"identifier_mismatch": 1}


def test_hallucinated_named_entity_and_relation_fail_closed(monkeypatch, tmp_path):
    payload = _valid_payload(
        summary=("Microsoft guarantees the API timeout is 30 seconds for request_id req-17.")
    )
    monkeypatch.setattr(
        "plastic_promise.core.semantic_chunk_enrichment.requests.post",
        lambda *args, **kwargs: _FakeChatResponse(json.dumps(payload)),
    )
    enricher = _enricher(monkeypatch, tmp_path)

    batch = enricher.prepare_chunks([_material()], source_text=_source())

    assert batch.embedding_texts == (_material().text,)
    assert batch.diagnostics["errors"] == {"ungrounded_summary": 1}


def test_truncated_json_and_ollama_error_fail_closed(monkeypatch, tmp_path):
    responses = [
        _FakeChatResponse('{"summary":"truncated"'),
        TimeoutError("ollama timeout"),
    ]

    def fake_post(*args, **kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("plastic_promise.core.semantic_chunk_enrichment.requests.post", fake_post)
    first = _enricher(monkeypatch, tmp_path)
    second = SemanticChunkEnricher(
        host="http://127.0.0.1:11434", cache_path=tmp_path / "other.db", mode="on"
    )

    invalid = first.prepare_chunks([_material()], source_text=_source())
    unavailable = second.prepare_chunks([_material()], source_text=_source())

    assert invalid.embedding_texts == unavailable.embedding_texts == (_material().text,)
    assert invalid.diagnostics["errors"] == {"invalid_json": 1}
    assert unavailable.diagnostics["errors"] == {"request_failed": 1}


def test_sqlite_cache_survives_new_instance(monkeypatch, tmp_path):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return _FakeChatResponse(json.dumps(_valid_payload()))

    monkeypatch.setattr("plastic_promise.core.semantic_chunk_enrichment.requests.post", fake_post)
    first = _enricher(monkeypatch, tmp_path)
    first_batch = first.prepare_chunks([_material()], source_text=_source())
    second = _enricher(monkeypatch, tmp_path)
    second_batch = second.prepare_chunks([_material()], source_text=_source())

    assert first_batch.embedding_texts == second_batch.embedding_texts
    assert calls == [1]
    assert second_batch.diagnostics["cache_hits"] == 1


def test_cache_key_includes_source_heading_model_and_prompt_version(monkeypatch, tmp_path):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return _FakeChatResponse(json.dumps(_valid_payload()))

    monkeypatch.setattr("plastic_promise.core.semantic_chunk_enrichment.requests.post", fake_post)
    base = _enricher(monkeypatch, tmp_path)
    changed_heading = ChunkMaterial(
        text=_material().text,
        kind=_material().kind,
        heading_path=("Operations",),
        source_start=_material().source_start,
        source_end=_material().source_end,
    )
    changed_source = _source().replace("30 seconds", "30 seconds exactly")

    base.prepare_chunks([_material()], source_text=_source())
    base.prepare_chunks([changed_heading], source_text=_source())
    base.prepare_chunks([_material()], source_text=changed_source)
    SemanticChunkEnricher(
        host="http://127.0.0.1:11434",
        cache_path=tmp_path / "enrichment.db",
        mode="on",
        model="qwen3:14b",
        model_digest=OTHER_DIGEST,
    ).prepare_chunks([_material()], source_text=_source())
    SemanticChunkEnricher(
        host="http://127.0.0.1:11434",
        cache_path=tmp_path / "enrichment.db",
        mode="on",
        prompt_version="semantic-chunk-enrichment-v2",
        model_digest=TEST_DIGEST,
    ).prepare_chunks([_material()], source_text=_source())

    assert len(calls) == 5


def test_shadow_queues_without_changing_embedding_text(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "plastic_promise.core.semantic_chunk_enrichment.requests.post",
        lambda *args, **kwargs: _FakeChatResponse(json.dumps(_valid_payload())),
    )
    enricher = _enricher(monkeypatch, tmp_path, mode="shadow")

    material = _material()
    batch = enricher.prepare_chunks([material], source_text=_source())

    assert batch.embedding_texts == (material.text,)
    assert batch.diagnostics["queued"] == 1
    assert enricher.wait_for_idle(timeout=2.0) is True

    cached = enricher.prepare_chunks([material], source_text=_source())
    assert cached.embedding_texts == (material.text,)
    assert cached.diagnostics["cache_hits"] == 1


def test_off_mode_is_noop_and_repeated_on_input_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "plastic_promise.core.semantic_chunk_enrichment.requests.post",
        lambda *args, **kwargs: _FakeChatResponse(json.dumps(_valid_payload())),
    )
    off = _enricher(monkeypatch, tmp_path, mode="off")
    on = SemanticChunkEnricher(
        host="http://127.0.0.1:11434", cache_path=tmp_path / "on.db", mode="on"
    )

    material = _material()
    off_batch = off.prepare_chunks([material], source_text=_source())
    first = on.prepare_chunks([material], source_text=_source())
    second = on.prepare_chunks([material], source_text=_source())

    assert off_batch.embedding_texts == (material.text,)
    assert first.embedding_texts == second.embedding_texts
    assert material == _material()


def test_off_mode_does_not_resolve_or_call_local_model(monkeypatch, tmp_path):
    def fail_get(*args, **kwargs):
        raise AssertionError("off mode must not resolve the enrichment model")

    def fail_post(*args, **kwargs):
        raise AssertionError("off mode must not call Ollama")

    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT", "off")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_CACHE_PATH", str(tmp_path / "enrichment.db"))
    monkeypatch.delenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST", raising=False)
    monkeypatch.setattr("plastic_promise.core.semantic_chunk_enrichment.requests.get", fail_get)
    monkeypatch.setattr("plastic_promise.core.semantic_chunk_enrichment.requests.post", fail_post)

    enricher = SemanticChunkEnricher(host="http://127.0.0.1:11434")
    batch = enricher.prepare_chunks([_material()], source_text=_source())

    assert batch.embedding_texts == (_material().text,)
    assert batch.diagnostics["model_digest"] == "unresolved"


def test_configured_digest_must_match_ollama_before_enrichment(monkeypatch, tmp_path):
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT", "shadow")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST", CONFIGURED_DIGEST)
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT_CACHE_PATH", str(tmp_path / "enrichment.db"))
    monkeypatch.setattr(
        "plastic_promise.core.semantic_chunk_enrichment.requests.get",
        lambda *args, **kwargs: _FakeTagsResponse(ACTUAL_DIGEST.removeprefix("sha256:")),
    )

    def fail_post(*args, **kwargs):
        raise AssertionError("digest mismatch must fail before chat")

    monkeypatch.setattr("plastic_promise.core.semantic_chunk_enrichment.requests.post", fail_post)
    enricher = SemanticChunkEnricher(host="http://127.0.0.1:11434")

    batch = enricher.prepare_chunks([_material()], source_text=_source())

    assert batch.embedding_texts == (_material().text,)
    assert batch.diagnostics["errors"] == {"model_digest_unavailable": 1}
    assert batch.diagnostics["queue_dropped"] == 1


def test_active_plan_persists_exact_chunk_inputs_and_fallback_status(monkeypatch, tmp_path):
    payload = _valid_payload(evidence=["not in source"])
    monkeypatch.setattr(
        "plastic_promise.core.semantic_chunk_enrichment.requests.post",
        lambda *args, **kwargs: _FakeChatResponse(json.dumps(payload)),
    )
    enricher = _enricher(monkeypatch, tmp_path)
    material = _material()

    batch = enricher.prepare_chunks([material], source_text=_source())
    plan_text = enricher.build_embedding_plan(_source(), [material], batch)
    plan = decode_embedding_plan(plan_text)

    assert plan["source_text_hash"]
    assert plan["model_identity"] == f"qwen3:8b@{TEST_DIGEST}"
    assert plan["chunks"] == [
        {
            "cache_key": batch.chunk_records[0]["cache_key"],
            "embedding_text": material.text,
            "error": "ungrounded_evidence",
            "heading_path": ["Retrieval"],
            "kind": "paragraph",
            "source_end": 64,
            "source_start": 12,
            "source_text": material.text,
            "status": "fallback",
        }
    ]


def test_same_key_concurrent_requests_use_single_flight(monkeypatch, tmp_path):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        time.sleep(0.1)
        return _FakeChatResponse(json.dumps(_valid_payload()))

    monkeypatch.setattr("plastic_promise.core.semantic_chunk_enrichment.requests.post", fake_post)
    first = _enricher(monkeypatch, tmp_path)
    second = _enricher(monkeypatch, tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(enricher.prepare_chunks, [_material()], source_text=_source())
            for enricher in (first, second)
        ]
        batches = [future.result() for future in futures]

    assert calls == [1]
    assert batches[0].embedding_texts == batches[1].embedding_texts
    assert sum(int(batch.diagnostics["cache_hits"]) for batch in batches) == 1
