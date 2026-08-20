from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from plastic_promise.local_inference_node.adapters import (
    LlamaCppEmbeddingAdapter,
    LlamaCppRerankingAdapter,
    LocalBgeReranker,
    NodeModelIdentityDriftError,
    NodeModelUnavailableError,
    OllamaEmbeddingAdapter,
    SentenceTransformersEmbeddingAdapter,
)
from plastic_promise.local_inference_node.cache_planner import main as cache_plan_main
from plastic_promise.local_inference_node.cache_policy import (
    CacheCleanupConditions,
    ModelCacheEntry,
    ModelCacheManifest,
    load_cache_manifest,
    plan_cache_cleanup,
)
from plastic_promise.local_inference_node.resources import (
    GIBIBYTE,
    NodeResourceEstimate,
    assess_node_resources,
    evaluate_node_capacity,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_sentence_transformers_adapter_uses_fixed_revision_and_local_files_only(tmp_path: Path):
    captured: dict[str, object] = {}

    class _FakeModel:
        def encode(self, texts, **kwargs):
            captured["encode"] = kwargs
            return [[1, 2], [3, 4]][: len(texts)]

    def loader(model_reference, **kwargs):
        captured["model_reference"] = model_reference
        captured.update(kwargs)
        return _FakeModel()

    adapter = SentenceTransformersEmbeddingAdapter(
        model_reference="/models/embedding",
        revision="pinned-revision",
        cache_dir=tmp_path,
        loader=loader,
    )

    assert adapter.embed_batch(["a", "b"]) == [[1.0, 2.0], [3.0, 4.0]]
    assert captured["revision"] == "pinned-revision"
    assert captured["cache_folder"] == str(tmp_path)
    assert captured["local_files_only"] is True
    assert captured["encode"] == {
        "batch_size": 2,
        "normalize_embeddings": True,
        "convert_to_numpy": False,
        "show_progress_bar": False,
    }


def test_sentence_transformers_adapter_does_not_leak_loader_failures():
    def loader(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("local filesystem layout")

    with pytest.raises(NodeModelUnavailableError, match="node_embedding_model_unavailable"):
        SentenceTransformersEmbeddingAdapter(
            model_reference="/models/embedding",
            revision="pinned-revision",
            loader=loader,
        )


def test_sentence_transformers_adapter_honors_declared_normalization():
    captured: dict[str, object] = {}

    class _FakeModel:
        def encode(self, _texts, **kwargs):
            captured.update(kwargs)
            return [[3.0, 4.0]]

    adapter = SentenceTransformersEmbeddingAdapter(
        model_reference="/models/embedding",
        revision="pinned-revision",
        normalization="none",
        loader=lambda *_args, **_kwargs: _FakeModel(),
    )

    assert adapter.embed_batch(["a"]) == [[3.0, 4.0]]
    assert captured["normalize_embeddings"] is False


def test_ollama_adapter_still_uses_the_unified_node_embedding_seam(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeOllamaEmbedder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def embed_batch(self, texts):
            return [[1.0] * 1024 for _text in texts]

    monkeypatch.setattr(
        "plastic_promise.local_inference_node.adapters.OllamaEmbedder", _FakeOllamaEmbedder
    )
    adapter = OllamaEmbeddingAdapter(
        host="http://127.0.0.1:11434",
        model="BAAI/bge-m3",
        expected_dimension=1024,
        expected_artifact_sha256="sha256:" + "a" * 64,
        identity_probe=lambda: "sha256:" + "a" * 64,
    )

    assert len(adapter.embed_batch(["one"])[0]) == 1024
    assert captured == {
        "host": "http://127.0.0.1:11434",
        "model": "BAAI/bge-m3",
        "expected_dim": 1024,
    }


def test_llama_cpp_embedding_adapter_uses_structured_embeddings_and_normalizes():
    requests: list[tuple[str, dict[str, object]]] = []

    class _Client:
        def post_json(self, path, payload):
            requests.append((path, payload))
            return SimpleNamespace(
                payload={
                    "model": "Qwen3-Embedding-4B-GGUF",
                    "data": [{"index": 0, "embedding": [3.0, 4.0]}],
                }
            )

    adapter = LlamaCppEmbeddingAdapter(
        base_url="http://127.0.0.1:19131",
        model="Qwen3-Embedding-4B-GGUF",
        expected_dimension=2,
        client=_Client(),
    )

    assert adapter.embed_batch(["text"]) == [[0.6, 0.8]]
    assert requests == [
        (
            "/v1/embeddings",
            {"model": "Qwen3-Embedding-4B-GGUF", "input": ["text"]},
        )
    ]


def test_llama_cpp_rerank_adapter_rejects_generation_and_returns_every_score():
    class _Client:
        def post_json(self, _path, _payload):
            return SimpleNamespace(
                payload={
                    "model": "Qwen3-Reranker-0.6B-GGUF",
                    "results": [
                        {"index": 1, "relevance_score": 0.8},
                        {"index": 0, "score": 0.2},
                    ],
                }
            )

    adapter = LlamaCppRerankingAdapter(
        base_url="http://127.0.0.1:19132",
        model="Qwen3-Reranker-0.6B-GGUF",
        client=_Client(),
    )
    assert adapter.rerank_tuples("q", [(10, "a"), (11, "b")], top_k=1) == [
        (10, 0.2),
        (11, 0.8),
    ]

    class _GenerationClient:
        def post_json(self, _path, _payload):
            return SimpleNamespace(payload={"content": "yes"})

    with pytest.raises(NodeModelUnavailableError, match="node_llama_cpp_rerank_response_invalid"):
        LlamaCppRerankingAdapter(
            base_url="http://127.0.0.1:19132",
            model="Qwen3-Reranker-0.6B-GGUF",
            client=_GenerationClient(),
        ).rerank_tuples("q", [(0, "a")], top_k=1)


def test_ollama_adapter_normalizes_only_when_declared(monkeypatch):
    class _FakeOllamaEmbedder:
        def __init__(self, **_kwargs):
            pass

        def embed_batch(self, _texts):
            return [[3.0, 4.0]]

    monkeypatch.setattr(
        "plastic_promise.local_inference_node.adapters.OllamaEmbedder", _FakeOllamaEmbedder
    )

    assert OllamaEmbeddingAdapter(
        host="http://127.0.0.1:11434",
        model="custom",
        expected_dimension=2,
        expected_artifact_sha256="sha256:" + "a" * 64,
        identity_probe=lambda: "sha256:" + "a" * 64,
    ).embed_batch(["one"]) == [[0.6, 0.8]]
    assert OllamaEmbeddingAdapter(
        host="http://127.0.0.1:11434",
        model="custom",
        expected_dimension=2,
        expected_artifact_sha256="sha256:" + "a" * 64,
        identity_probe=lambda: "sha256:" + "a" * 64,
        normalization="none",
    ).embed_batch(["one"]) == [[3.0, 4.0]]


def test_ollama_adapter_rejects_a_model_tag_that_changes_after_startup(monkeypatch):
    class _FakeOllamaEmbedder:
        def __init__(self, **_kwargs):
            pass

        def embed_batch(self, _texts):
            return [[1.0, 0.0]]

    monkeypatch.setattr(
        "plastic_promise.local_inference_node.adapters.OllamaEmbedder", _FakeOllamaEmbedder
    )
    observed = iter(("sha256:" + "a" * 64, "sha256:" + "b" * 64))
    adapter = OllamaEmbeddingAdapter(
        host="http://127.0.0.1:11434",
        model="mutable-tag",
        expected_dimension=2,
        expected_artifact_sha256="sha256:" + "a" * 64,
        identity_probe=lambda: next(observed),
    )

    with pytest.raises(NodeModelIdentityDriftError, match="node_ollama_model_identity_drift"):
        adapter.embed_batch(["one"])


def test_bge_reranker_scores_every_candidate_without_applying_top_k():
    class _FakeTensor:
        def to(self, _device):
            return self

        def view(self, _shape):
            return self

        def detach(self):
            return self

        def cpu(self):
            return self

        def float(self):
            return self

        def tolist(self):
            return [0.2, 0.8]

    class _FakeTokenizer:
        def __call__(self, queries, documents, **kwargs):
            assert queries == ["needle", "needle"]
            assert documents == ["low", "high"]
            assert kwargs["max_length"] == 256
            return {"input_ids": _FakeTensor()}

    class _FakeModel:
        def to(self, device):
            assert device == "cpu"
            return self

        def eval(self):
            return None

        def __call__(self, **kwargs):
            assert "input_ids" in kwargs
            return type("Output", (), {"logits": _FakeTensor()})()

    class _FakeTorch:
        @staticmethod
        def inference_mode():
            return nullcontext()

        @staticmethod
        def sigmoid(value):
            return value

        cuda = type("Cuda", (), {"is_available": staticmethod(lambda: False)})

    def loader(*args, **kwargs):
        del args, kwargs
        return _FakeTokenizer(), _FakeModel(), _FakeTorch()

    adapter = LocalBgeReranker(
        model_reference="/models/rerank",
        revision="pinned-revision",
        device="cpu",
        max_length=256,
        loader=loader,
    )

    assert adapter.rerank_tuples("needle", [(4, "low"), (1, "high")], top_k=1) == [
        (4, 0.2),
        (1, 0.8),
    ]


def test_cache_cleanup_preserves_active_fallback_and_busy_node_artifacts():
    now = datetime(2026, 8, 6, 4, 30, tzinfo=timezone.utc)
    entries = [
        ModelCacheEntry("active", "models/active", now - timedelta(days=10)),
        ModelCacheEntry("fallback", "models/fallback", now - timedelta(days=10)),
        ModelCacheEntry("old", "models/old", now - timedelta(hours=25)),
        ModelCacheEntry("recent", "models/recent", now - timedelta(hours=23)),
    ]
    ready = CacheCleanupConditions(True, False, False, now)
    busy = CacheCleanupConditions(True, True, False, now)

    assert plan_cache_cleanup(
        entries,
        active_revision="active",
        fallback_revision="fallback",
        conditions=ready,
    ).eligible_paths == ("models/old",)
    assert (
        plan_cache_cleanup(
            entries,
            active_revision="active",
            fallback_revision="fallback",
            conditions=ready,
        ).run_at_local_time
        == "04:30"
    )
    assert (
        plan_cache_cleanup(
            entries,
            active_revision="active",
            fallback_revision="fallback",
            conditions=busy,
        ).skipped_reason
        == "model_download_active"
    )


def test_cache_manifest_is_strict_secret_free_metadata():
    manifest = ModelCacheManifest.from_json(
        {
            "schema_version": "plastic-promise-local-node-cache/v1",
            "active_revision": "active",
            "fallback_revision": "fallback",
            "entries": [
                {
                    "revision": "old",
                    "relative_path": "models/old",
                    "last_used_at": "2026-08-01T00:00:00Z",
                }
            ],
        }
    )

    assert manifest.public_json()["entries"][0]["relative_path"] == "models/old"
    with pytest.raises(ValueError, match="node_cache_manifest_fields_invalid"):
        ModelCacheManifest.from_json({"unexpected": "field"})
    with pytest.raises(ValueError, match="node_cache_manifest_rollback_required"):
        ModelCacheManifest("same", "same", ())


def test_daily_cache_planner_reads_manifest_and_status_without_deleting(tmp_path: Path, capsys):
    manifest_path = tmp_path / "cache-manifest.json"
    status_path = tmp_path / "cache-status.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "plastic-promise-local-node-cache/v1",
                "active_revision": "active",
                "fallback_revision": "fallback",
                "entries": [
                    {
                        "revision": "old",
                        "relative_path": "models/old",
                        "last_used_at": "2026-08-01T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    status_path.write_text(
        json.dumps(
            {
                "node_healthy": True,
                "model_download_active": False,
                "index_rebuild_active": False,
            }
        ),
        encoding="utf-8",
    )

    assert load_cache_manifest(manifest_path).entries[0].relative_path == "models/old"
    assert (
        cache_plan_main(
            [
                "--manifest",
                str(manifest_path),
                "--status",
                str(status_path),
                "--now",
                "2026-08-06T04:30:00Z",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "eligible_paths": ["models/old"],
        "run_at_local_time": "04:30",
        "skipped_reason": None,
    }


def test_cache_entries_reject_path_traversal_and_resource_preflight_is_a_hard_gate():
    with pytest.raises(ValueError, match="node_cache_relative_path_invalid"):
        ModelCacheEntry("old", "../outside", datetime.now(timezone.utc))

    estimate = NodeResourceEstimate(
        image_download_bytes=1 * GIBIBYTE,
        image_unpack_bytes=1 * GIBIBYTE,
        model_artifact_bytes=2 * GIBIBYTE,
        model_cache_bytes=1 * GIBIBYTE,
        rollback_bytes=1 * GIBIBYTE,
        existing_runtime_bytes=0,
    )
    denied = evaluate_node_capacity(
        total_bytes=100 * GIBIBYTE,
        free_bytes=25 * GIBIBYTE,
        estimate=estimate,
    )
    allowed = evaluate_node_capacity(
        total_bytes=100 * GIBIBYTE,
        free_bytes=40 * GIBIBYTE,
        estimate=estimate,
    )

    assert denied.required_post_apply_free_bytes == 20 * GIBIBYTE
    assert denied.projected_free_bytes == 19 * GIBIBYTE
    assert denied.allowed is False
    assert allowed.allowed is True


def test_resource_filesystem_preflight_delegates_to_the_pure_capacity_policy(
    monkeypatch, tmp_path: Path
):
    from plastic_promise.local_inference_node import resources

    estimate = NodeResourceEstimate(1, 1, 1, 1, 1, 1)
    total_bytes = 100 * GIBIBYTE
    free_bytes = 40 * GIBIBYTE
    monkeypatch.setattr(
        resources.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=total_bytes, free=free_bytes),
    )

    assert assess_node_resources(tmp_path, estimate) == evaluate_node_capacity(
        total_bytes=total_bytes,
        free_bytes=free_bytes,
        estimate=estimate,
    )
