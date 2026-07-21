import hashlib
from types import SimpleNamespace

import pytest

import plastic_promise.core.embedder as embedder_module
from plastic_promise.core.chunking import chunk_manifest_hash
from plastic_promise.core.embedder import (
    CachedEmbedder,
    Embedder,
    OllamaEmbedder,
    StructureAwareEmbedder,
)
from plastic_promise.core.memory_index import (
    SUMMARY_POLICY,
    build_index_material,
    effective_embedding_model_name,
    index_metadata,
    metadata_with_index_material,
    prepare_index_material,
    read_persisted_index_material,
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


class _RecordingProvider(Embedder):
    def __init__(self):
        self.calls = []

    def embed(self, text):
        self.calls.append(text)
        return [1.0, 0.0]

    def embed_batch(self, texts):
        return [self.embed(text) for text in texts]

    @property
    def dim(self):
        return 2

    @property
    def model_name(self):
        return "recording-provider"


@pytest.fixture
def isolated_rust_chunk_parity_gate():
    embedder_module._reset_rust_chunk_parity_gate()
    try:
        yield
    finally:
        embedder_module._reset_rust_chunk_parity_gate()


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


def test_structure_v1_default_hard_limit_matches_model_context(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json["prompt"])
        return _FakeEmbeddingResponse([1.0, 0.0])

    monkeypatch.setattr("plastic_promise.core.embedder.requests.post", fake_post)
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "512")
    monkeypatch.delenv("EMBEDDER_STRUCTURE_HARD_CHARS", raising=False)
    source = '{"principle_observations":"' + ("evidence, " * 180) + '"}'

    embedder = OllamaEmbedder(host="http://127.0.0.1:11434")
    embedder.embed(source)

    assert len(calls) > 1
    assert max(map(len, calls)) <= 512
    assert "hard_chars=512" in embedder.index_model_name


def test_structure_aware_wrapper_applies_to_non_ollama_provider(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENGINE", "python")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "24")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "40")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_MAX_CHUNKS", "16")
    provider = _RecordingProvider()
    embedder = StructureAwareEmbedder(provider)

    vector = embedder.embed("# 检索\n\n第一段需要结构上下文。\n\n## 证据\n\n尾部证据必须保留。")

    assert vector == [1.0, 0.0]
    assert len(provider.calls) >= 2
    assert any("检索" in chunk for chunk in provider.calls)
    assert "尾部证据" in provider.calls[-1]
    assert embedder.last_chunking_diagnostics["effective_engine"] == "python"
    assert "|chunking=structure-v1" in embedder.index_model_name


def test_rust_structure_gate_validates_once_and_rechecks_identity_or_config(
    monkeypatch,
    isolated_rust_chunk_parity_gate,
):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENGINE", "rust")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "64")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "128")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_MAX_CHUNKS", "8")
    python_calls = []
    rust_calls = []
    original_python_parser = embedder_module.structure_aware_chunks
    probe_materials = {
        (64, 128, 8): embedder_module.limit_chunk_materials(
            original_python_parser(
                embedder_module.STRUCTURE_CHUNK_PARITY_PROBE,
                target_chars=64,
                hard_chars=128,
            ),
            8,
        ),
        (80, 160, 8): embedder_module.limit_chunk_materials(
            original_python_parser(
                embedder_module.STRUCTURE_CHUNK_PARITY_PROBE,
                target_chars=80,
                hard_chars=160,
            ),
            8,
        ),
    }
    assert {material.kind for material in probe_materials[(64, 128, 8)]} >= {
        "paragraph",
        "list",
        "table",
        "code",
    }
    assert any(material.heading_path for material in probe_materials[(64, 128, 8)])
    assert "𠮷" in embedder_module.STRUCTURE_CHUNK_PARITY_PROBE

    def counting_python_parser(text, *, target_chars, hard_chars):
        python_calls.append((text, target_chars, hard_chars))
        return original_python_parser(
            text,
            target_chars=target_chars,
            hard_chars=hard_chars,
        )

    def fake_rust_materials(text, *, target_chars, hard_chars, max_chunks):
        rust_calls.append((text, target_chars, hard_chars, max_chunks))
        if text == embedder_module.STRUCTURE_CHUNK_PARITY_PROBE:
            return probe_materials[(target_chars, hard_chars, max_chunks)]
        return [
            embedder_module.ChunkMaterial(
                text=text,
                kind="paragraph",
                heading_path=(),
                source_start=0,
                source_end=len(text),
            )
        ]

    def fake_rust_core(suffix):
        def projection(*args):
            return []

        return SimpleNamespace(
            __name__="context_engine_core",
            __file__=f"F:/Agent/Memory system/.tmp/context_engine_core_{suffix}.pyd",
            __version__="test",
            structure_chunk_projection=projection,
        )

    rust_core = [fake_rust_core("a")]
    monkeypatch.setattr(embedder_module, "structure_aware_chunks", counting_python_parser)
    monkeypatch.setattr(embedder_module, "_rust_chunk_materials", fake_rust_materials)
    monkeypatch.setattr(
        "plastic_promise.core.rust_extension.load_context_engine_core",
        lambda: rust_core[0],
    )
    embedder = StructureAwareEmbedder(_RecordingProvider())

    embedder.embed("first parity sample")
    embedder.embed("second rust hot path")

    assert [call[0] for call in python_calls] == [
        embedder_module.STRUCTURE_CHUNK_PARITY_PROBE
    ]
    assert [call[0] for call in rust_calls] == [
        embedder_module.STRUCTURE_CHUNK_PARITY_PROBE,
        "first parity sample",
        "second rust hot path",
    ]
    assert embedder.last_chunking_diagnostics["effective_engine"] == "rust"
    assert embedder.last_chunking_diagnostics["engine_fallback_reason"] == ""

    rust_core[0] = fake_rust_core("b")
    embedder.embed("new extension identity")
    assert [call[0] for call in python_calls][-1] == (
        embedder_module.STRUCTURE_CHUNK_PARITY_PROBE
    )

    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "80")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "160")
    StructureAwareEmbedder(_RecordingProvider()).embed("new chunk configuration")
    assert [call[0] for call in python_calls][-1] == (
        embedder_module.STRUCTURE_CHUNK_PARITY_PROBE
    )
    assert len(python_calls) == 3


@pytest.mark.parametrize("first_business_text", ["", "plain business paragraph"])
def test_rust_structure_mismatch_stably_falls_back_to_python(
    monkeypatch,
    isolated_rust_chunk_parity_gate,
    first_business_text,
):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENGINE", "rust")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "64")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "128")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_MAX_CHUNKS", "8")
    rust_calls = []
    rust_core = SimpleNamespace(
        __name__="context_engine_core",
        __file__="F:/Agent/Memory system/.tmp/context_engine_core_mismatch.pyd",
        structure_chunk_projection=lambda *args: [],
    )

    def mismatched_rust_materials(text, **kwargs):
        rust_calls.append(text)
        return [embedder_module.ChunkMaterial("mismatch", "paragraph", (), 0, len(text))]

    monkeypatch.setattr(embedder_module, "_rust_chunk_materials", mismatched_rust_materials)
    monkeypatch.setattr(
        "plastic_promise.core.rust_extension.load_context_engine_core",
        lambda: rust_core,
    )
    embedder = StructureAwareEmbedder(_RecordingProvider())

    embedder.embed(first_business_text)
    first_diagnostics = embedder.last_chunking_diagnostics
    embedder.embed("second fallback sample")

    assert rust_calls == [embedder_module.STRUCTURE_CHUNK_PARITY_PROBE]
    assert first_diagnostics["effective_engine"] == "python"
    assert first_diagnostics["engine_fallback_reason"] == "rust_python_chunking_mismatch"
    assert embedder.last_chunking_diagnostics["effective_engine"] == "python"
    assert (
        embedder.last_chunking_diagnostics["engine_fallback_reason"]
        == "rust_python_chunking_mismatch"
    )


def test_rust_structure_hot_path_error_latches_python_fallback(
    monkeypatch,
    isolated_rust_chunk_parity_gate,
):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENGINE", "rust")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "64")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "128")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_MAX_CHUNKS", "8")
    rust_calls = []
    original_python_parser = embedder_module.structure_aware_chunks
    probe_materials = embedder_module.limit_chunk_materials(
        original_python_parser(
            embedder_module.STRUCTURE_CHUNK_PARITY_PROBE,
            target_chars=64,
            hard_chars=128,
        ),
        8,
    )
    rust_core = SimpleNamespace(
        __name__="context_engine_core",
        __file__="F:/Agent/Memory system/.tmp/context_engine_core_error.pyd",
        structure_chunk_projection=lambda *args: [],
    )

    def flaky_rust_materials(text, **kwargs):
        rust_calls.append(text)
        if text == embedder_module.STRUCTURE_CHUNK_PARITY_PROBE:
            return probe_materials
        if text == "runtime failure sample":
            raise RuntimeError("rust_chunking_runtime_failure")
        return [embedder_module.ChunkMaterial(text, "paragraph", (), 0, len(text))]

    monkeypatch.setattr(embedder_module, "_rust_chunk_materials", flaky_rust_materials)
    monkeypatch.setattr(
        "plastic_promise.core.rust_extension.load_context_engine_core",
        lambda: rust_core,
    )
    embedder = StructureAwareEmbedder(_RecordingProvider())

    embedder.embed("initial parity sample")
    embedder.embed("runtime failure sample")
    embedder.embed("latched fallback sample")

    assert rust_calls == [
        embedder_module.STRUCTURE_CHUNK_PARITY_PROBE,
        "initial parity sample",
        "runtime failure sample",
    ]
    assert embedder.last_chunking_diagnostics["effective_engine"] == "python"
    assert (
        embedder.last_chunking_diagnostics["engine_fallback_reason"]
        == "rust_chunking_runtime_failure"
    )


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


def test_structure_v1_identity_applies_to_non_ollama_providers(monkeypatch):
    class LocalProvider:
        index_model_name = "local-model"

    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "128")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "256")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_MAX_CHUNKS", "12")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS", "4096")

    identity = effective_embedding_model_name(LocalProvider())

    assert identity.startswith("local-model|chunking=structure-v1")
    assert "target_chars=128" in identity
    assert "max_chunks=12" in identity


def test_structure_v1_manifest_is_persisted_with_index_material(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("EMBEDDER_CHUNK_CHARS", "24")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_HARD_CHARS", "48")
    monkeypatch.setenv("EMBEDDER_STRUCTURE_MAX_CHUNKS", "16")
    embedder = CachedEmbedder(OllamaEmbedder(host="http://127.0.0.1:11434"))
    source = "# 检索\n\n第一段。\n\n## 证据\n\nTAIL-EVIDENCE"

    material = prepare_index_material({"content": source}, embedder=embedder, policy="legacy")
    metadata = metadata_with_index_material({}, material)
    row = {
        "embedding_text": material.vector_text,
        "embedding_hash": material.embedding_hash,
        "search_text": material.search_text,
        "metadata_json": metadata,
    }
    restored = read_persisted_index_material(row, model_name=material.model_name)

    manifest = metadata["memory_index"]["chunk_manifest"]
    assert manifest["schema_version"] == "structure-v1"
    assert manifest["chunk_count"] == len(manifest["chunks"])
    assert manifest["chunks"][-1]["text"].endswith("TAIL-EVIDENCE")
    assert len(metadata["memory_index"]["chunk_manifest_hash"]) == 64
    assert restored is not None
    assert restored.chunk_manifest == manifest


def test_structure_v1_persisted_material_without_manifest_fails_closed(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    material = build_index_material(
        {"content": "# 检索\n\n必须保留切片证据。"},
        model_name=(
            "mxbai-embed-large|chunking=structure-v1|target_chars=512|hard_chars=1024|"
            "max_chunks=64|max_source_chars=2000000|budget=characters-fallback"
        ),
    )
    metadata = metadata_with_index_material({}, material)
    metadata["memory_index"].pop("chunk_manifest")
    metadata["memory_index"].pop("chunk_manifest_hash")
    row = {
        "embedding_text": material.vector_text,
        "embedding_hash": material.embedding_hash,
        "search_text": material.search_text,
        "metadata_json": metadata,
    }

    assert read_persisted_index_material(row, model_name=material.model_name) is None


def test_structure_v1_manifest_must_be_bound_to_persisted_vector_text(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    model_name = (
        "mxbai-embed-large|chunking=structure-v1|target_chars=512|hard_chars=1024|"
        "max_chunks=64|max_source_chars=2000000|budget=characters-fallback"
    )
    material = build_index_material(
        {"content": "# 原始证据\n\n这才是实际向量文本。"},
        model_name=model_name,
    )
    unrelated = build_index_material(
        {"content": "# 无关证据\n\n这是另一份可自洽但不属于本行的切片。"},
        model_name=model_name,
    )
    metadata = metadata_with_index_material({}, material)
    metadata["memory_index"]["chunk_manifest"] = unrelated.chunk_manifest
    metadata["memory_index"]["chunk_manifest_hash"] = chunk_manifest_hash(
        unrelated.chunk_manifest
    )
    row = {
        "embedding_text": material.vector_text,
        "embedding_hash": material.embedding_hash,
        "search_text": material.search_text,
        "metadata_json": metadata,
    }

    assert read_persisted_index_material(row, model_name=model_name) is None


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


def test_structure_enrichment_manifest_stays_bound_to_source_text(monkeypatch, tmp_path):
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
    assert is_embedding_plan(material.vector_text)
    assert material.chunk_manifest is not None
    assert material.chunk_manifest["source_hash"] == hashlib.sha256(source.encode()).hexdigest()
    assert material.chunk_manifest["chunks"][0]["text"] == "Retrieval\nThe API timeout is 30 seconds for request_id req-17."

    metadata = metadata_with_index_material({}, material)
    restored = read_persisted_index_material(
        {
            "embedding_text": material.vector_text,
            "embedding_hash": material.embedding_hash,
            "search_text": material.search_text,
            "metadata_json": metadata,
        },
        model_name=material.model_name,
    )
    assert restored is not None
    assert restored.chunk_manifest == material.chunk_manifest


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
    embedder.close()


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
