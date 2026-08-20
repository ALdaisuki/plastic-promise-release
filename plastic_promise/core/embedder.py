"""Plastic Promise Embedder — text-to-vector with provider abstraction.

The governed production path is an authenticated compute node.  Its local
default is llama.cpp and its cloud JSON/embedding providers are selected by
the compute-node projection.  This module remains a legacy compatibility
facade for callers that have not migrated to ``MemoryIndexNodeRuntime``:
without an explicit provider it uses the OpenAI-compatible contract and
degrades to text-only fallback when credentials are absent.  Ollama is
compatibility-only and must be selected explicitly with
``EMBEDDER_PROVIDER=ollama``.

Provider selection is explicit:
  1. openai-compatible — configured cloud endpoint (legacy facade default)
  2. ollama  — local HTTP server, explicit compatibility mode
  2. local   — sentence-transformers, in-process, zero HTTP
  4. openai — official OpenAI endpoint
  5. fallback — zero vectors, text-only retrieval

Environment variables:
  EMBEDDER_PROVIDER=ollama|local|openai|openai-compatible|cloud|fallback
                                              (default: openai-compatible)
  EMBEDDER_BASE_URL=https://.../v1
  EMBEDDER_API_KEY=<EnvironmentFile only>
  EMBEDDER_MODEL=text-embedding-v4
  EMBEDDER_MODEL_REVISION=<provider model revision>
  EMBEDDER_DIMENSION=1024
  EMBEDDER_SEND_DIMENSIONS=1      (set 0 for fixed/native-dimension APIs)
  EMBEDDER_BATCH_SIZE=32
  EMBEDDER_MODEL=mxbai-embed-large  (ollama model name)
  EMBEDDER_LOCAL_MODEL=BAAI/bge-large-zh-v1.5  (sentence-transformers model)
  OLLAMA_HOST=http://localhost:11434
  EMBEDDER_CACHE_SIZE=256          (default: 256, set to 0 to disable)
  EMBEDDER_CACHE_TTL=300           (TTL in seconds, default: 300)
  EMBEDDER_TIMEOUT=5               (HTTP timeout for Ollama/OpenAI, default: 5)
  PP_MEMORY_CHUNKING=off|shadow|structure-v1  (default: off for legacy callers;
  the launcher and compute-node projection default to structure-v1)
  PP_MEMORY_CHUNK_ENRICHMENT=off|shadow|on  (default: off for legacy callers;
  the launcher defaults to shadow; structure-v1 only)
  PP_MEMORY_CHUNK_ENRICHMENT_MODEL=<compute-node structured-JSON model>
      (qwen3:8b is compatibility-only when the Ollama adapter is explicit)
  PP_MEMORY_CHUNK_ENRICHMENT_TIMEOUT=45
  EMBEDDER_CHUNK_CHARS=512         (legacy size / structure-v1 soft target)
  EMBEDDER_MAX_CHUNKS=8            (legacy cap only)
  EMBEDDER_STRUCTURE_HARD_CHARS=1024  (structure-v1 oversized-block limit)
  EMBEDDER_STRUCTURE_MAX_CHUNKS=64    (structure-v1 request cap)
  EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS=2000000  (structure-v1 input guard)
"""

import asyncio
import hashlib
import logging
import math
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

import requests

from plastic_promise.core.chunking import (
    STRUCTURE_CHUNK_PARITY_PROBE,
    ChunkMaterial,
    has_uncovered_content,
    legacy_character_chunks,
    limit_chunk_materials,
    shadow_chunking_diagnostics,
    structure_aware_chunks,
)
from plastic_promise.core.cost_telemetry import TokenCostPolicy
from plastic_promise.core.semantic_chunk_enrichment import (
    SemanticChunkEnricher,
    decode_embedding_plan,
    is_embedding_plan,
)

_DEFAULT_EMBEDDING_BATCH_SIZE = 32
_HARD_MAX_EMBEDDING_BATCH_SIZE = 256
_DEFAULT_EMBEDDING_INPUT_BYTES = 64 * 1024
_HARD_MAX_EMBEDDING_INPUT_BYTES = 1024 * 1024
_DEFAULT_EMBEDDING_TOTAL_INPUT_BYTES = 1024 * 1024
_HARD_MAX_EMBEDDING_TOTAL_INPUT_BYTES = 8 * 1024 * 1024
_DEFAULT_EMBEDDING_REQUEST_BYTES = 2 * 1024 * 1024
_HARD_MAX_EMBEDDING_REQUEST_BYTES = 16 * 1024 * 1024
_DEFAULT_EMBEDDING_TOTAL_TIMEOUT_SECONDS = 60.0
_HARD_MAX_EMBEDDING_TOTAL_TIMEOUT_SECONDS = 600.0


class Embedder(ABC):
    """Abstract text-to-vector embedder."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Convert text to an embedding vector."""

    async def aembed(self, text: str) -> list[float]:
        """Async wrapper: runs embed() in thread pool to avoid event-loop blocking."""
        return await asyncio.to_thread(self.embed, text)

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch embed multiple texts."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Embedding dimension."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model identifier."""

    @property
    def index_model_name(self) -> str:
        """Versioned identity for derived index material."""
        return self.model_name

    def prepare_index_text(self, text: str) -> str:
        """Prepare exact persisted document material; queries should call embed directly."""
        return text

    @property
    def supports_native_batch(self) -> bool:
        """Whether ``embed_batch`` avoids per-text provider calls."""
        return False

    def close(self) -> None:
        """Release optional provider resources."""
        return None


class CachedEmbedder(Embedder):
    """LRU embedding cache wrapper — eliminates redundant Ollama/API calls.

    Caches embeddings by content hash. Thread-safe with TTL-based expiry.
    Configurable via EMBEDDER_CACHE_SIZE (default 256) and EMBEDDER_CACHE_TTL (default 300s).
    Set EMBEDDER_CACHE_SIZE=0 to disable.

    Provides both sync ``embed()`` and async ``embed_async()`` — the async variant
    runs the delegate's HTTP call in ``asyncio.to_thread()`` to avoid blocking the
    event loop (critical for SSE/MCP request handlers).
    """

    def __init__(self, delegate: Embedder, max_size: int = None, ttl_seconds: int = None) -> None:
        self._delegate = delegate
        self._max_size = (
            max_size if max_size is not None else int(os.environ.get("EMBEDDER_CACHE_SIZE", "256"))
        )
        self._ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else int(os.environ.get("EMBEDDER_CACHE_TTL", "300"))
        )
        self._cache: dict[str, tuple[list[float], float]] = {}  # hash -> (vector, timestamp)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _key(self, text: str) -> str:
        namespace = f"{self.index_model_name}\0dim={self.dim}\0"
        return hashlib.sha256((namespace + text).encode("utf-8")).hexdigest()

    def embed(self, text: str) -> list[float]:
        if self._max_size <= 0:
            return self._delegate.embed(text)
        key = self._key(text)
        now = time.time()
        with self._lock:
            if key in self._cache:
                vec, ts = self._cache[key]
                if now - ts < self._ttl:
                    self._hits += 1
                    return vec
                del self._cache[key]
        self._misses += 1
        vec = self._delegate.embed(text)

        # Runtime fallback: if delegate returns zero vectors and is not
        # already FallbackEmbedder, try Ollama as live recovery path.
        # This detects lazy-init failures (e.g., LocalSentenceEmbedder
        # constructor succeeded but _lazy_load() failed at embed time).
        runtime_delegate = getattr(self._delegate, "_delegate", self._delegate)
        if (
            vec
            and not any(v != 0.0 for v in vec)
            and isinstance(runtime_delegate, LocalSentenceEmbedder)
            and getattr(runtime_delegate, "_allow_ollama_recovery", True)
        ):
            import logging

            _log = logging.getLogger("plastic-promise.embedder")
            _log.warning(
                "CachedEmbedder: delegate %s returned zero vector, "
                "attempting runtime fallback to Ollama",
                type(self._delegate).__name__,
            )
            try:
                recovery_dim = getattr(runtime_delegate, "dim", None)
                replacement: Embedder = OllamaEmbedder(expected_dim=recovery_dim)
                if isinstance(self._delegate, StructureAwareEmbedder):
                    replacement = StructureAwareEmbedder(replacement)
                ollama_vec = replacement.embed(text)
                if ollama_vec and any(v != 0.0 for v in ollama_vec):
                    _log.info(
                        "CachedEmbedder: Ollama runtime fallback succeeded, "
                        "switching delegate permanently"
                    )
                    previous = self._delegate
                    self._delegate = replacement
                    previous.close()
                    vec = ollama_vec
                else:
                    replacement.close()
            except Exception as e:
                _log.warning("CachedEmbedder: Ollama runtime fallback also failed: %s", e)

        with self._lock:
            if len(self._cache) >= self._max_size:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
                del self._cache[oldest_key]
            if vec and any(value != 0.0 for value in vec):
                self._cache[key] = (vec, now)
        return vec

    async def embed_async(self, text: str) -> list[float]:
        """Async variant: runs the delegate HTTP call in a thread to avoid
        blocking the asyncio event loop.  Cache hit returns immediately;
        cache miss offloads the blocking ``requests.post()`` to a thread.

        Only valid when called from inside a running event loop.
        """
        import asyncio as _asyncio

        # Fast path: cache hit (no I/O)
        if self._max_size > 0:
            key = self._key(text)
            now = time.time()
            with self._lock:
                if key in self._cache:
                    vec, ts = self._cache[key]
                    if now - ts < self._ttl:
                        self._hits += 1
                        return vec
        # Slow path: delegate embed in thread so event loop stays responsive
        return await _asyncio.to_thread(self.embed, text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch embed with cache check per text."""
        if self._max_size <= 0:
            return self._delegate.embed_batch(texts)
        results = []
        uncached: dict[str, dict[str, object]] = {}
        now = time.time()
        for i, text in enumerate(texts):
            key = self._key(text)
            with self._lock:
                if key in self._cache:
                    vec, ts = self._cache[key]
                    if now - ts < self._ttl:
                        self._hits += 1
                        results.append((i, vec))
                        continue
                    del self._cache[key]
            pending = uncached.setdefault(key, {"text": text, "indices": []})
            pending["indices"].append(i)
            self._misses += 1

        if uncached:
            pending_items = list(uncached.items())
            new_vecs = self._delegate.embed_batch(
                [str(pending["text"]) for _, pending in pending_items]
            )
            if len(new_vecs) != len(pending_items):
                raise RuntimeError("embedding_response_count_mismatch")
            with self._lock:
                for (key, pending), vec in zip(pending_items, new_vecs, strict=True):
                    if vec and any(value != 0.0 for value in vec):
                        if len(self._cache) >= self._max_size:
                            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
                            del self._cache[oldest_key]
                        self._cache[key] = (vec, now)
                    for index in pending["indices"]:
                        results.append((index, vec))

        results.sort(key=lambda x: x[0])
        return [v for _, v in results]

    @property
    def supports_native_batch(self) -> bool:
        return bool(getattr(self._delegate, "supports_native_batch", False))

    @property
    def dim(self) -> int:
        return self._delegate.dim

    @property
    def model_name(self) -> str:
        return self._delegate.model_name

    @property
    def index_model_name(self) -> str:
        return self._delegate.index_model_name

    def prepare_index_text(self, text: str) -> str:
        return self._delegate.prepare_index_text(text)

    def close(self) -> None:
        close = getattr(self._delegate, "close", None)
        if callable(close):
            close()

    @property
    def last_chunking_diagnostics(self) -> dict[str, object]:
        diagnostics = getattr(self._delegate, "last_chunking_diagnostics", None)
        return dict(diagnostics) if isinstance(diagnostics, dict) else {}

    @property
    def last_index_preparation_diagnostics(self) -> dict[str, object]:
        diagnostics = getattr(self._delegate, "last_index_preparation_diagnostics", None)
        return dict(diagnostics) if isinstance(diagnostics, dict) else {}

    @property
    def stats(self) -> dict:
        delegate_stats = getattr(self._delegate, "stats", {})
        provider_stats = dict(delegate_stats) if isinstance(delegate_stats, Mapping) else {}
        with self._lock:
            total = self._hits + self._misses
            return {
                **provider_stats,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / max(total, 1), 3),
                "cache_size": len(self._cache),
                "max_size": self._max_size,
                "ttl_seconds": self._ttl,
            }


class OllamaEmbedder(Embedder):
    """Local Ollama embedding provider.

    Default model: mxbai-embed-large (1024 dim, MTEB top-tier, multilingual).
    Requires Ollama running at OLLAMA_HOST.
    """

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        *,
        expected_dim: int | None = None,
    ) -> None:
        endpoint_role = os.environ.get("PP_ENDPOINT_ROLE", "").strip()
        if endpoint_role and endpoint_role != "pp-compute-node":
            raise ValueError("inference_requires_compute_node")
        if expected_dim is None and (
            "PP_EMBEDDING_DIM" in os.environ or "EMBEDDER_DIMENSION" in os.environ
        ):
            expected_dim = _embedding_schema_dimension()
        self._expected_dim = _optional_expected_dimension(expected_dim)
        raw = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        # 0.0.0.0 is a server bind address — client must connect to localhost
        raw = raw.replace("0.0.0.0", "127.0.0.1")
        if "://" in raw:
            self._host = raw
        else:
            # Plain host[:port] — add scheme and default port if missing
            if ":" in raw:
                self._host = f"http://{raw}"
            else:
                self._host = f"http://{raw}:11434"
        self._model = model or os.getenv("EMBEDDER_MODEL", "mxbai-embed-large")
        self._chunk_chars = _int_env("EMBEDDER_CHUNK_CHARS", 512, minimum=1)
        self._max_chunks = _int_env("EMBEDDER_MAX_CHUNKS", 8, minimum=1)
        self._chunking_mode = os.getenv("PP_MEMORY_CHUNKING", "off").strip().lower()
        if self._chunking_mode not in {"off", "shadow", "structure-v1"}:
            logging.warning("Unknown PP_MEMORY_CHUNKING=%r; using off", self._chunking_mode)
            self._chunking_mode = "off"
        self._structure_hard_chars = _int_env(
            "EMBEDDER_STRUCTURE_HARD_CHARS",
            self._chunk_chars,
            minimum=self._chunk_chars,
        )
        self._structure_max_chunks = _int_env("EMBEDDER_STRUCTURE_MAX_CHUNKS", 64, minimum=1)
        self._structure_max_source_chars = _int_env(
            "EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS", 2_000_000, minimum=1
        )
        self._last_chunking_diagnostics: dict[str, object] = {}
        self._last_index_preparation_diagnostics: dict[str, object] = {}
        self._chunk_enricher = SemanticChunkEnricher(host=self._host)
        if self._chunk_enricher.mode != "off" and self._chunking_mode != "structure-v1":
            logging.warning(
                "PP_MEMORY_CHUNK_ENRICHMENT=%s requires PP_MEMORY_CHUNKING=structure-v1; "
                "enrichment is inactive",
                self._chunk_enricher.mode,
            )

    def embed(self, text: str) -> list[float]:
        if is_embedding_plan(text):
            plan = decode_embedding_plan(text)
            if self._chunk_enricher.mode != "on":
                raise ValueError("embedding_plan_mode_mismatch")
            if plan.get("model_identity") != self._chunk_enricher.model_identity:
                raise ValueError("embedding_plan_model_mismatch")
            plan_chunks = plan["chunks"]
            assert isinstance(plan_chunks, list)
            chunks = [str(chunk["embedding_text"]) for chunk in plan_chunks]
            self._last_chunking_diagnostics = {
                "mode": "embedding-plan-v1",
                "chunk_count": len(chunks),
                "source_text_hash": plan.get("source_text_hash", ""),
                "model_identity": plan.get("model_identity", ""),
                "enriched": sum(1 for chunk in plan_chunks if chunk.get("status") == "enriched"),
                "fallbacks": sum(1 for chunk in plan_chunks if chunk.get("status") == "fallback"),
            }
        elif self._chunking_mode == "structure-v1":
            if len(text or "") > self._structure_max_source_chars:
                self._last_chunking_diagnostics = {
                    "mode": "structure-v1",
                    "source_chars": len(text or ""),
                    "resource_limited": True,
                    "error": "structure_chunking_source_too_large",
                }
                raise ValueError("structure_chunking_source_too_large")
            all_materials = structure_aware_chunks(
                text,
                target_chars=self._chunk_chars,
                hard_chars=self._structure_hard_chars,
            )
            resource_limited = len(all_materials) > self._structure_max_chunks
            materials = limit_chunk_materials(all_materials, self._structure_max_chunks)
            chunks = [material.text for material in materials]
            last_source_end = max((material.source_end for material in materials), default=0)
            meaningful_source_end = len((text or "").rstrip())
            self._last_chunking_diagnostics = {
                "mode": "structure-v1",
                "source_chars": len(text or ""),
                "chunk_count": len(materials),
                "covered_source_chars": sum(
                    max(material.source_end - material.source_start, 0) for material in materials
                ),
                "last_source_end": last_source_end,
                "budget_unit": "characters-fallback",
                "truncated": last_source_end < meaningful_source_end
                or resource_limited
                or has_uncovered_content(text or "", materials)
                or any(material.context_truncated for material in materials),
                "max_chunks": self._structure_max_chunks,
                "resource_limited": resource_limited,
                "context_truncated": any(material.context_truncated for material in materials),
            }
        else:
            chunks = legacy_character_chunks(text, self._chunk_chars, self._max_chunks)
            if self._chunking_mode == "shadow":
                self._last_chunking_diagnostics = shadow_chunking_diagnostics(
                    text,
                    target_chars=self._chunk_chars,
                    hard_chars=self._structure_hard_chars,
                    max_chunks=self._max_chunks,
                    legacy_chunks=chunks,
                    max_source_chars=self._structure_max_source_chars,
                )
            else:
                self._last_chunking_diagnostics = {
                    "mode": "legacy",
                    "source_chars": len(text or ""),
                    "chunk_count": len(chunks),
                    "covered_source_chars": sum(len(chunk) for chunk in chunks),
                    "budget_unit": "characters",
                    "truncated": len(text or "") > sum(len(chunk) for chunk in chunks),
                }
        if len(chunks) == 1:
            return self._embed_chunk(chunks[0])
        return _mean_pool_vectors([self._embed_chunk(chunk) for chunk in chunks])

    def prepare_index_text(self, text: str) -> str:
        """Prepare exact document-only material before SQLite hashing/persistence."""

        if self._chunking_mode != "structure-v1" or self._chunk_enricher.mode == "off":
            self._last_index_preparation_diagnostics = {
                "mode": self._chunk_enricher.mode,
                "active": False,
            }
            return text
        if len(text or "") > self._structure_max_source_chars:
            raise ValueError("structure_chunking_source_too_large")
        all_materials = structure_aware_chunks(
            text,
            target_chars=self._chunk_chars,
            hard_chars=self._structure_hard_chars,
        )
        materials = limit_chunk_materials(all_materials, self._structure_max_chunks)
        batch = self._chunk_enricher.prepare_chunks(materials, source_text=text or "")
        self._last_index_preparation_diagnostics = dict(batch.diagnostics)
        if self._chunk_enricher.mode == "shadow":
            return text
        return self._chunk_enricher.build_embedding_plan(text or "", materials, batch)

    def close(self) -> None:
        self._chunk_enricher.close()

    @property
    def last_chunking_diagnostics(self) -> dict[str, object]:
        return dict(self._last_chunking_diagnostics)

    @property
    def last_index_preparation_diagnostics(self) -> dict[str, object]:
        return dict(self._last_index_preparation_diagnostics)

    def _embed_chunk(self, text: str) -> list[float]:
        resp = requests.post(
            f"{self._host}/api/embeddings",
            json={"model": self._model, "prompt": text},
            timeout=float(os.getenv("EMBEDDER_TIMEOUT", "5")),
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError("embedding_response_schema_invalid")
        vector = payload.get("embedding")
        if not isinstance(vector, list):
            raise RuntimeError("embedding_response_schema_invalid")
        return _validate_embedding_vector(vector, self._expected_dim)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    @property
    def dim(self) -> int:
        return self._expected_dim or 1024

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def index_model_name(self) -> str:
        if self._chunking_mode != "structure-v1":
            return self._model
        identity = (
            f"{self._model}|chunking=structure-v1"
            f"|target_chars={self._chunk_chars}"
            f"|hard_chars={self._structure_hard_chars}"
            f"|max_chunks={self._structure_max_chunks}"
            f"|max_source_chars={self._structure_max_source_chars}"
            "|budget=characters-fallback"
        )
        if self._chunk_enricher.mode == "on":
            identity = f"{identity}|{self._chunk_enricher.index_identity}"
        return identity


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), minimum)
    except (TypeError, ValueError):
        return max(default, minimum)


def _embedding_schema_dimension(default: int = 1024) -> int:
    """Resolve the fixed LanceDB vector dimension from the process contract."""

    # PP_EMBEDDING_DIM is the canonical project-wide override.  Keep the
    # provider-specific EMBEDDER_DIMENSION as a compatible fallback when the
    # canonical variable is absent, so a cloud-only environment cannot report
    # one dimension while constructing an incompatible schema.
    raw = os.environ.get(
        "PP_EMBEDDING_DIM",
        os.environ.get("EMBEDDER_DIMENSION", str(default)),
    )
    try:
        dimension = int(raw)
    except (TypeError, ValueError):
        raise ValueError("embedding_dimension_invalid") from None
    if dimension <= 0:
        raise ValueError("embedding_dimension_invalid")
    return dimension


def _embedding_send_dimensions(explicit: bool | None = None) -> bool:
    """Resolve whether cloud requests include the optional dimensions field."""

    if explicit is not None:
        if not isinstance(explicit, bool):
            raise ValueError("embedding_send_dimensions_invalid")
        return explicit
    raw = os.environ.get("EMBEDDER_SEND_DIMENSIONS", "1").strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError("embedding_send_dimensions_invalid")


def _optional_expected_dimension(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("embedding_dimension_invalid")
    return value


def _validate_embedding_vector(vector: object, expected_dim: int | None) -> list[float]:
    if not isinstance(vector, list):
        raise RuntimeError("embedding_response_schema_invalid")
    if expected_dim is not None and len(vector) != expected_dim:
        raise RuntimeError("embedding_response_dimension_mismatch")
    validated: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError("embedding_response_value_invalid")
        try:
            converted = float(value)
        except (OverflowError, ValueError):
            raise RuntimeError("embedding_response_value_invalid") from None
        if not math.isfinite(converted):
            raise RuntimeError("embedding_response_value_invalid")
        validated.append(converted)
    if expected_dim is not None and not any(value != 0.0 for value in validated):
        raise RuntimeError("embedding_response_zero_vector")
    return validated


def _bounded_positive_int_setting(
    explicit: int | None,
    *,
    env_name: str,
    default: int,
    maximum: int,
    reason: str,
) -> int:
    raw: object = explicit if explicit is not None else os.environ.get(env_name, str(default))
    if isinstance(raw, bool):
        raise ValueError(reason)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(reason) from None
    if value <= 0:
        raise ValueError(reason)
    return min(value, maximum)


def _bounded_positive_float_setting(
    explicit: float | None,
    *,
    env_name: str,
    default: float,
    maximum: float,
    reason: str,
) -> float:
    raw: object = explicit if explicit is not None else os.environ.get(env_name, str(default))
    if isinstance(raw, bool):
        raise ValueError(reason)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(reason) from None
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(reason)
    return min(value, maximum)


def _embedding_chunks(text: str, chunk_chars: int, max_chunks: int) -> list[str]:
    """Compatibility wrapper for callers that used the old private helper."""

    return legacy_character_chunks(text, chunk_chars, max_chunks)


def _mean_pool_vectors(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    if dim == 0:
        return []
    totals = [0.0] * dim
    count = 0
    for vec in vectors:
        if len(vec) != dim:
            continue
        count += 1
        for i, value in enumerate(vec):
            totals[i] += float(value)
    if count == 0:
        return []
    pooled = [value / count for value in totals]
    norm = math.sqrt(sum(value * value for value in pooled))
    if norm <= 0.0:
        return pooled
    return [value / norm for value in pooled]


def _structure_chunking_settings() -> dict[str, int | str]:
    target = _int_env("EMBEDDER_CHUNK_CHARS", 512, minimum=1)
    return {
        "mode": os.environ.get("PP_MEMORY_CHUNKING", "off").strip().casefold(),
        "engine": os.environ.get("PP_MEMORY_CHUNK_ENGINE", "python").strip().casefold(),
        "target_chars": target,
        "hard_chars": _int_env(
            "EMBEDDER_STRUCTURE_HARD_CHARS",
            target,
            minimum=target,
        ),
        "max_chunks": _int_env("EMBEDDER_STRUCTURE_MAX_CHUNKS", 64, minimum=1),
        "max_source_chars": _int_env("EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS", 2_000_000, minimum=1),
    }


_RUST_CHUNK_PARITY_GATE_LOCK = threading.Lock()
_RUST_CHUNK_PARITY_GATES: dict[
    tuple[object, ...],
    tuple[str, str],
] = {}


def _reset_rust_chunk_parity_gate() -> None:
    """Clear process-local Rust parity decisions (primarily for isolated tests)."""

    with _RUST_CHUNK_PARITY_GATE_LOCK:
        _RUST_CHUNK_PARITY_GATES.clear()


def _rust_chunk_extension_identity() -> tuple[object, ...]:
    """Identify the loaded projection without making the binary path the sole key."""

    from plastic_promise.core.rust_extension import load_context_engine_core

    try:
        rust_core = load_context_engine_core()
    except Exception as exc:
        # A stable unavailable identity avoids repeating either parser while the
        # same loader failure persists. If the extension later loads, its module
        # identity produces a new gate key and parity is checked again.
        return (
            "unavailable",
            id(load_context_engine_core),
            type(exc).__module__,
            type(exc).__qualname__,
            str(exc),
        )

    spec = getattr(rust_core, "__spec__", None)
    origin = getattr(rust_core, "__file__", None) or getattr(spec, "origin", None) or ""
    projection = getattr(rust_core, "structure_chunk_projection", None)
    return (
        "loaded",
        str(getattr(rust_core, "__name__", type(rust_core).__qualname__)),
        str(origin),
        str(getattr(rust_core, "__version__", "")),
        id(rust_core),
        id(projection),
    )


def _rust_chunk_parity_gate_key(
    *,
    target_chars: int,
    hard_chars: int,
    max_chunks: int,
) -> tuple[object, ...]:
    return (
        _rust_chunk_extension_identity(),
        target_chars,
        hard_chars,
        max_chunks,
        id(structure_aware_chunks),
        id(limit_chunk_materials),
        id(_rust_chunk_materials),
    )


def _python_structure_materials(
    text: str,
    *,
    target_chars: int,
    hard_chars: int,
    max_chunks: int,
) -> list[ChunkMaterial]:
    return limit_chunk_materials(
        structure_aware_chunks(text, target_chars=target_chars, hard_chars=hard_chars),
        max_chunks,
    )


def _rust_chunk_materials(
    text: str,
    *,
    target_chars: int,
    hard_chars: int,
    max_chunks: int,
) -> list[ChunkMaterial]:
    """Load Rust's canonical projection and validate its public shape."""

    from plastic_promise.core.rust_extension import load_context_engine_core

    projection = getattr(load_context_engine_core(), "structure_chunk_projection", None)
    if not callable(projection):
        raise RuntimeError("rust_chunking_api_unavailable")
    rows = projection(text, target_chars, hard_chars, max_chunks)
    if not isinstance(rows, (list, tuple)):
        raise RuntimeError("rust_chunking_result_invalid")
    materials: list[ChunkMaterial] = []
    for row in rows:
        try:
            item = dict(row)
            heading_path = tuple(str(value) for value in item.get("heading_path", []))
            material = ChunkMaterial(
                text=str(item["text"]),
                kind=str(item["kind"]),
                heading_path=heading_path,
                source_start=int(item["source_start"]),
                source_end=int(item["source_end"]),
                context_truncated=bool(item.get("context_truncated", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("rust_chunking_result_invalid") from exc
        if material.source_start < 0 or material.source_end < material.source_start:
            raise RuntimeError("rust_chunking_span_invalid")
        materials.append(material)
    return materials or [ChunkMaterial("", "empty", (), 0, 0)]


def _effective_structure_materials(
    text: str,
    settings: dict[str, int | str],
) -> tuple[list[ChunkMaterial], str, str]:
    """Return chunks, effective engine, and a stable fallback reason."""

    target = int(settings["target_chars"])
    hard = int(settings["hard_chars"])
    max_chunks = int(settings["max_chunks"])
    requested = str(settings["engine"])
    if requested != "rust":
        return (
            _python_structure_materials(
                text,
                target_chars=target,
                hard_chars=hard,
                max_chunks=max_chunks,
            ),
            "python",
            "",
        )

    gate_key = _rust_chunk_parity_gate_key(
        target_chars=target,
        hard_chars=hard,
        max_chunks=max_chunks,
    )
    with _RUST_CHUNK_PARITY_GATE_LOCK:
        gate = _RUST_CHUNK_PARITY_GATES.get(gate_key)
        if gate is None:
            python_materials = _python_structure_materials(
                STRUCTURE_CHUNK_PARITY_PROBE,
                target_chars=target,
                hard_chars=hard,
                max_chunks=max_chunks,
            )
            try:
                rust_materials = _rust_chunk_materials(
                    STRUCTURE_CHUNK_PARITY_PROBE,
                    target_chars=target,
                    hard_chars=hard,
                    max_chunks=max_chunks,
                )
                if rust_materials != python_materials:
                    raise RuntimeError("rust_python_chunking_mismatch")
            except Exception as exc:
                fallback_reason = str(exc) or "rust_chunking_failed"
                _RUST_CHUNK_PARITY_GATES[gate_key] = ("fallback", fallback_reason)
                gate = ("fallback", fallback_reason)
            else:
                _RUST_CHUNK_PARITY_GATES[gate_key] = ("matched", "")
                gate = ("matched", "")

    gate_status, fallback_reason = gate
    if gate_status == "fallback":
        return (
            _python_structure_materials(
                text,
                target_chars=target,
                hard_chars=hard,
                max_chunks=max_chunks,
            ),
            "python",
            fallback_reason,
        )

    try:
        rust_materials = _rust_chunk_materials(
            text,
            target_chars=target,
            hard_chars=hard,
            max_chunks=max_chunks,
        )
    except Exception as exc:
        fallback_reason = str(exc) or "rust_chunking_failed"
        with _RUST_CHUNK_PARITY_GATE_LOCK:
            current_gate = _RUST_CHUNK_PARITY_GATES.get(gate_key)
            if current_gate is None or current_gate[0] == "matched":
                _RUST_CHUNK_PARITY_GATES[gate_key] = ("fallback", fallback_reason)
            else:
                fallback_reason = current_gate[1]
        return (
            _python_structure_materials(
                text,
                target_chars=target,
                hard_chars=hard,
                max_chunks=max_chunks,
            ),
            "python",
            fallback_reason,
        )

    # Another request can discover a Rust runtime failure while this call is
    # executing. Honor that process-level fallback before returning its result.
    with _RUST_CHUNK_PARITY_GATE_LOCK:
        current_gate = _RUST_CHUNK_PARITY_GATES.get(gate_key)
    if current_gate is not None and current_gate[0] == "fallback":
        return (
            _python_structure_materials(
                text,
                target_chars=target,
                hard_chars=hard,
                max_chunks=max_chunks,
            ),
            "python",
            current_gate[1],
        )
    return rust_materials, "rust", ""


class StructureAwareEmbedder(Embedder):
    """Provider-neutral structured chunking wrapper.

    Ollama historically owned the chunking implementation itself.  Keeping the
    wrapper at the provider boundary makes ``full`` mode behave the same for
    Ollama, OpenAI, local sentence-transformers, and the zero-vector fallback.
    """

    def __init__(self, delegate: Embedder) -> None:
        self._delegate = delegate
        self._settings = _structure_chunking_settings()
        self._last_chunking_diagnostics: dict[str, object] = {}
        self._last_index_preparation_diagnostics: dict[str, object] = {}
        inherited_enricher = getattr(delegate, "_chunk_enricher", None)
        self._owns_chunk_enricher = inherited_enricher is None
        self._chunk_enricher = inherited_enricher or SemanticChunkEnricher(
            host=getattr(delegate, "_host", None)
        )

    def embed(self, text: str) -> list[float]:
        chunks, diagnostics = self._embedding_chunks(text)
        self._last_chunking_diagnostics = diagnostics
        return self._embed_chunks(chunks)

    def _embedding_chunks(self, text: str) -> tuple[list[str], dict[str, object]]:
        if is_embedding_plan(text):
            plan = decode_embedding_plan(text)
            if self._chunk_enricher.mode != "on":
                raise ValueError("embedding_plan_mode_mismatch")
            if plan.get("model_identity") != self._chunk_enricher.model_identity:
                raise ValueError("embedding_plan_model_mismatch")
            if (
                plan.get("prompt_hash") != self._chunk_enricher.prompt_hash
                or plan.get("schema_hash") != self._chunk_enricher.schema_hash
            ):
                raise ValueError("embedding_plan_contract_mismatch")
            plan_chunks = plan["chunks"]
            assert isinstance(plan_chunks, list)
            chunks = [str(chunk["embedding_text"]) for chunk in plan_chunks]
            diagnostics = {
                "mode": "embedding-plan-v1",
                "chunk_count": len(chunks),
                "source_text_hash": plan.get("source_text_hash", ""),
                "model_identity": plan.get("model_identity", ""),
                "enriched": sum(1 for chunk in plan_chunks if chunk.get("status") == "enriched"),
                "fallbacks": sum(1 for chunk in plan_chunks if chunk.get("status") == "fallback"),
            }
            return chunks, diagnostics
        if len(text or "") > int(self._settings["max_source_chars"]):
            self._last_chunking_diagnostics = {
                "mode": "structure-v1",
                "source_chars": len(text or ""),
                "resource_limited": True,
                "error": "structure_chunking_source_too_large",
            }
            raise ValueError("structure_chunking_source_too_large")

        materials, effective_engine, fallback_reason = _effective_structure_materials(
            text or "", self._settings
        )
        source = text or ""
        last_source_end = max((material.source_end for material in materials), default=0)
        coverage_gap = has_uncovered_content(source, materials)
        diagnostics = {
            "mode": "structure-v1",
            "requested_engine": str(self._settings["engine"]),
            "effective_engine": effective_engine,
            "engine_fallback_reason": fallback_reason,
            "source_chars": len(source),
            "chunk_count": len(materials),
            "covered_source_chars": sum(
                max(material.source_end - material.source_start, 0) for material in materials
            ),
            "last_source_end": last_source_end,
            "budget_unit": "characters-fallback",
            "truncated": last_source_end < len(source.rstrip())
            or coverage_gap
            or any(material.context_truncated for material in materials),
            "max_chunks": int(self._settings["max_chunks"]),
            "resource_limited": coverage_gap,
            "context_truncated": any(material.context_truncated for material in materials),
        }
        return [material.text for material in materials], diagnostics

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not all(isinstance(text, str) for text in texts):
            raise TypeError("embedding_input_must_be_text")

        flat_chunks: list[str] = []
        spans: list[tuple[int, int]] = []
        last_diagnostics: dict[str, object] = {}
        for text in texts:
            chunks, diagnostics = self._embedding_chunks(text)
            if not chunks:
                raise RuntimeError("embedding_chunks_empty")
            start = len(flat_chunks)
            flat_chunks.extend(chunks)
            spans.append((start, len(flat_chunks)))
            last_diagnostics = diagnostics

        vectors = self._embed_chunk_vectors(flat_chunks)
        if len(vectors) != len(flat_chunks):
            raise RuntimeError("embedding_response_count_mismatch")
        self._last_chunking_diagnostics = {
            **last_diagnostics,
            "batched": True,
            "batch_input_count": len(texts),
            "batch_chunk_count": len(flat_chunks),
        }
        return [
            vectors[start] if end - start == 1 else _mean_pool_vectors(vectors[start:end])
            for start, end in spans
        ]

    def prepare_index_text(self, text: str) -> str:
        if self._chunk_enricher.mode == "off":
            prepare = getattr(self._delegate, "prepare_index_text", None)
            return prepare(text) if callable(prepare) else text
        if len(text or "") > int(self._settings["max_source_chars"]):
            raise ValueError("structure_chunking_source_too_large")
        materials, effective_engine, fallback_reason = _effective_structure_materials(
            text or "", self._settings
        )
        batch = self._chunk_enricher.prepare_chunks(materials, source_text=text or "")
        self._last_index_preparation_diagnostics = {
            **batch.diagnostics,
            "requested_engine": str(self._settings["engine"]),
            "effective_engine": effective_engine,
            "engine_fallback_reason": fallback_reason,
        }
        if self._chunk_enricher.mode == "shadow":
            return text
        return self._chunk_enricher.build_embedding_plan(text or "", materials, batch)

    def _embed_chunks(self, chunks: list[str]) -> list[float]:
        vectors = self._embed_chunk_vectors(chunks)
        return vectors[0] if len(vectors) == 1 else _mean_pool_vectors(vectors)

    def _embed_chunk_vectors(self, chunks: list[str]) -> list[list[float]]:
        # Ollama exposes a low-level request method; using it avoids re-entering
        # its legacy chunking branch. Other providers retain one shared batch
        # deadline while still producing one vector per canonical chunk.
        low_level = getattr(self._delegate, "_embed_chunk", None)
        if callable(low_level):
            return [low_level(chunk) for chunk in chunks]
        return self._delegate.embed_batch(chunks)

    @property
    def supports_native_batch(self) -> bool:
        return bool(getattr(self._delegate, "supports_native_batch", False))

    @property
    def dim(self) -> int:
        return self._delegate.dim

    @property
    def model_name(self) -> str:
        return self._delegate.model_name

    @property
    def index_model_name(self) -> str:
        base = str(self._delegate.index_model_name)
        if "|chunking=structure-v1" in base:
            identity = base
        else:
            identity = (
                f"{base}|chunking=structure-v1"
                f"|target_chars={self._settings['target_chars']}"
                f"|hard_chars={self._settings['hard_chars']}"
                f"|max_chunks={self._settings['max_chunks']}"
                f"|max_source_chars={self._settings['max_source_chars']}"
                "|budget=characters-fallback"
            )
        if self._chunk_enricher.mode == "on" and "|enrichment=" not in identity:
            identity = f"{identity}|{self._chunk_enricher.index_identity}"
        return identity

    @property
    def last_chunking_diagnostics(self) -> dict[str, object]:
        return dict(self._last_chunking_diagnostics)

    @property
    def last_index_preparation_diagnostics(self) -> dict[str, object]:
        if self._last_index_preparation_diagnostics:
            return dict(self._last_index_preparation_diagnostics)
        diagnostics = getattr(self._delegate, "last_index_preparation_diagnostics", {})
        return dict(diagnostics) if isinstance(diagnostics, dict) else {}

    @property
    def stats(self) -> dict[str, object]:
        stats = getattr(self._delegate, "stats", {})
        return dict(stats) if isinstance(stats, Mapping) else {}

    def close(self) -> None:
        if self._owns_chunk_enricher:
            self._chunk_enricher.close()
        self._delegate.close()


class OpenAICompatibleEmbedder(Embedder):
    """Strict OpenAI-compatible cloud embedding provider.

    The provider owns one reusable HTTP client and validates every response
    before a vector can enter derived state. Raw input and response bodies are
    intentionally absent from diagnostics.
    """

    _DEFAULT_BASE_URL = ""
    _DEFAULT_MODEL = "text-embedding-v4"
    _DEFAULT_DIM = 1024
    _PROVIDER_IDENTITY = "openai-compatible"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        model_revision: str | None = None,
        dim: int | None = None,
        send_dimensions: bool | None = None,
        batch_size: int | None = None,
        max_input_bytes: int | None = None,
        max_total_input_bytes: int | None = None,
        max_request_bytes: int | None = None,
        total_timeout_seconds: float | None = None,
        client: object | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = (api_key if api_key is not None else os.getenv("EMBEDDER_API_KEY", "")).strip()
        if not self._key and client is None:
            raise ValueError("embedding_api_key_missing")

        self._base_url = (
            base_url or os.getenv("EMBEDDER_BASE_URL", self._DEFAULT_BASE_URL)
        ).strip()
        if not self._base_url:
            raise ValueError("embedding_base_url_missing")
        self._path = os.getenv("EMBEDDER_PATH", "/embeddings").strip() or "/embeddings"
        self._model = (model or os.getenv("EMBEDDER_MODEL", self._DEFAULT_MODEL)).strip()
        self._model_revision = (
            model_revision or os.getenv("EMBEDDER_MODEL_REVISION", self._model)
        ).strip()
        self._dim = int(
            dim if dim is not None else _embedding_schema_dimension(default=self._DEFAULT_DIM)
        )
        schema_dim = _embedding_schema_dimension(default=self._DEFAULT_DIM)
        if self._dim <= 0:
            raise ValueError("embedding_dimension_invalid")
        if self._dim != schema_dim:
            raise ValueError("embedding_dimension_schema_mismatch")
        self._send_dimensions = _embedding_send_dimensions(send_dimensions)
        self._batch_size = _bounded_positive_int_setting(
            batch_size,
            env_name="EMBEDDER_BATCH_SIZE",
            default=_DEFAULT_EMBEDDING_BATCH_SIZE,
            maximum=_HARD_MAX_EMBEDDING_BATCH_SIZE,
            reason="embedding_batch_size_invalid",
        )
        self._max_input_bytes = _bounded_positive_int_setting(
            max_input_bytes,
            env_name="EMBEDDER_MAX_INPUT_BYTES",
            default=_DEFAULT_EMBEDDING_INPUT_BYTES,
            maximum=_HARD_MAX_EMBEDDING_INPUT_BYTES,
            reason="embedding_max_input_bytes_invalid",
        )
        self._max_total_input_bytes = _bounded_positive_int_setting(
            max_total_input_bytes,
            env_name="EMBEDDER_MAX_TOTAL_INPUT_BYTES",
            default=_DEFAULT_EMBEDDING_TOTAL_INPUT_BYTES,
            maximum=_HARD_MAX_EMBEDDING_TOTAL_INPUT_BYTES,
            reason="embedding_max_total_input_bytes_invalid",
        )
        self._max_request_bytes = _bounded_positive_int_setting(
            max_request_bytes,
            env_name="EMBEDDER_MAX_REQUEST_BYTES",
            default=_DEFAULT_EMBEDDING_REQUEST_BYTES,
            maximum=_HARD_MAX_EMBEDDING_REQUEST_BYTES,
            reason="embedding_max_request_bytes_invalid",
        )
        self._total_timeout_seconds = _bounded_positive_float_setting(
            total_timeout_seconds,
            env_name="EMBEDDER_TOTAL_TIMEOUT",
            default=_DEFAULT_EMBEDDING_TOTAL_TIMEOUT_SECONDS,
            maximum=_HARD_MAX_EMBEDDING_TOTAL_TIMEOUT_SECONDS,
            reason="embedding_total_timeout_invalid",
        )
        self._clock = clock
        if not self._model or not self._model_revision:
            raise ValueError("embedding_model_identity_missing")
        self._cost_policy = TokenCostPolicy.from_environment(
            "EMBEDDER",
            reason_prefix="embedding",
        )

        self._client = client or self._build_http_client()
        self._stats_lock = threading.Lock()
        self._requests = 0
        self._input_count = 0
        self._input_tokens = 0
        self._total_tokens = 0
        self._token_usage_complete = True
        self._latency_ms = 0.0

    def _build_http_client(self):
        from plastic_promise.core.provider_http import ProviderHTTPClient, ProviderHTTPPolicy

        policy = ProviderHTTPPolicy(
            timeout_seconds=float(os.getenv("EMBEDDER_TIMEOUT", "15")),
            total_timeout_seconds=self._total_timeout_seconds,
            max_retries=_int_env("EMBEDDER_MAX_RETRIES", 3, minimum=0),
            backoff_base_seconds=float(os.getenv("EMBEDDER_RETRY_BACKOFF", "0.25")),
            backoff_max_seconds=float(os.getenv("EMBEDDER_RETRY_BACKOFF_MAX", "4")),
            circuit_failure_threshold=_int_env("EMBEDDER_CIRCUIT_FAILURE_THRESHOLD", 5, minimum=1),
            circuit_recovery_seconds=float(os.getenv("EMBEDDER_CIRCUIT_RECOVERY_SECONDS", "30")),
            max_request_bytes=self._max_request_bytes,
            max_response_bytes=_int_env(
                "EMBEDDER_MAX_RESPONSE_BYTES", 16 * 1024 * 1024, minimum=1024
            ),
        )
        return ProviderHTTPClient(
            provider="embedding",
            base_url=self._base_url,
            api_key=self._key,
            policy=policy,
        )

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not all(isinstance(text, str) for text in texts):
            raise TypeError("embedding_input_must_be_text")
        total_input_bytes = 0
        for text in texts:
            try:
                input_bytes = len(text.encode("utf-8"))
            except UnicodeEncodeError:
                raise ValueError("embedding_input_utf8_invalid") from None
            if input_bytes > self._max_input_bytes:
                raise ValueError("embedding_input_too_large")
            total_input_bytes += input_bytes
            if total_input_bytes > self._max_total_input_bytes:
                raise ValueError("embedding_total_input_too_large")

        deadline = self._clock() + self._total_timeout_seconds
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            if self._clock() >= deadline:
                from plastic_promise.core.provider_http import ProviderHTTPError

                raise ProviderHTTPError("provider_http_deadline_exceeded")
            batch = texts[start : start + self._batch_size]
            request_payload: dict[str, object] = {
                "model": self._model,
                "input": batch,
            }
            if self._send_dimensions:
                request_payload["dimensions"] = self._dim
            result = self._client.post_json(
                self._path,
                request_payload,
                deadline=deadline,
            )
            if self._clock() >= deadline:
                from plastic_promise.core.provider_http import ProviderHTTPError

                raise ProviderHTTPError("provider_http_deadline_exceeded")
            payload = getattr(result, "payload", result)
            vectors.extend(self._validated_vectors(payload, len(batch)))
            self._record_usage(result, payload, len(batch))
        return vectors

    @property
    def supports_native_batch(self) -> bool:
        return True

    def _validated_vectors(self, payload: object, expected_count: int) -> list[list[float]]:
        if not isinstance(payload, Mapping):
            raise RuntimeError("embedding_response_schema_invalid")
        # Providers commonly echo the effective model.  If they do, bind that
        # response identity to the configured model; accepting a different
        # model would make the persisted index identity untrustworthy.  Some
        # compatible endpoints omit the field, so omission remains supported.
        response_model = payload.get("model")
        if response_model is not None:
            if not isinstance(response_model, str) or not response_model.strip():
                raise RuntimeError("embedding_response_model_invalid")
            if response_model.strip() != self._model:
                raise RuntimeError("embedding_response_model_mismatch")
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != expected_count:
            raise RuntimeError("embedding_response_count_mismatch")

        ordered: list[list[float] | None] = [None] * expected_count
        for row in rows:
            if not isinstance(row, Mapping):
                raise RuntimeError("embedding_response_schema_invalid")
            index = row.get("index")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < expected_count
                or ordered[index] is not None
            ):
                raise RuntimeError("embedding_response_index_invalid")
            raw_vector = row.get("embedding")
            if not isinstance(raw_vector, list) or len(raw_vector) != self._dim:
                raise RuntimeError("embedding_response_dimension_mismatch")
            vector: list[float] = []
            for value in raw_vector:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise RuntimeError("embedding_response_value_invalid")
                try:
                    converted = float(value)
                except (OverflowError, ValueError):
                    raise RuntimeError("embedding_response_value_invalid") from None
                if not math.isfinite(converted):
                    raise RuntimeError("embedding_response_value_invalid")
                vector.append(converted)
            if not any(value != 0.0 for value in vector):
                raise RuntimeError("embedding_response_zero_vector")
            ordered[index] = vector
        if any(vector is None for vector in ordered):
            raise RuntimeError("embedding_response_index_invalid")
        return [vector for vector in ordered if vector is not None]

    def _record_usage(self, result: object, payload: object, input_count: int) -> None:
        usage = payload.get("usage", {}) if isinstance(payload, Mapping) else {}
        usage = usage if isinstance(usage, Mapping) else {}

        def nonnegative_int(name: str) -> tuple[int, bool]:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return int(value), True
            return 0, False

        prompt_tokens, has_prompt_tokens = nonnegative_int("prompt_tokens")
        supplied_input_tokens, has_input_tokens = nonnegative_int("input_tokens")
        supplied_total_tokens, has_total_tokens = nonnegative_int("total_tokens")
        if has_prompt_tokens:
            input_tokens = prompt_tokens
        elif has_input_tokens:
            input_tokens = supplied_input_tokens
        elif has_total_tokens:
            # Embedding responses have no completion side, so total tokens are
            # valid input-cost evidence when the provider omits an input alias.
            input_tokens = supplied_total_tokens
        else:
            input_tokens = 0
        total_tokens = supplied_total_tokens if has_total_tokens else input_tokens
        usage_complete = has_prompt_tokens or has_input_tokens or has_total_tokens
        latency = getattr(result, "latency_ms", 0.0)
        latency_ms = float(latency) if isinstance(latency, (int, float)) else 0.0
        with self._stats_lock:
            self._requests += 1
            self._input_count += input_count
            self._input_tokens += input_tokens
            self._total_tokens += total_tokens
            self._token_usage_complete = self._token_usage_complete and usage_complete
            self._latency_ms += max(latency_ms, 0.0)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def index_model_name(self) -> str:
        endpoint = hashlib.sha256(f"{self._base_url}\0{self._path}".encode()).hexdigest()
        dimensions_mode = "" if self._send_dimensions else "|dimensions=native"
        return (
            f"{self._model}|provider={self._PROVIDER_IDENTITY}"
            f"|revision={self._model_revision}|dim={self._dim}|endpoint_sha256={endpoint}"
            f"{dimensions_mode}"
        )

    @property
    def stats(self) -> dict[str, object]:
        with self._stats_lock:
            return {
                "provider": self._PROVIDER_IDENTITY,
                "model": self._model,
                "revision": self._model_revision,
                "dimension": self._dim,
                "dimensions_parameter": "explicit" if self._send_dimensions else "native",
                "requests": self._requests,
                "inputs": self._input_count,
                "input_tokens": self._input_tokens,
                "total_tokens": self._total_tokens,
                "latency_ms": round(self._latency_ms, 3),
                **self._cost_policy.telemetry(
                    self._input_tokens if self._token_usage_complete else None,
                    cost_basis="input_tokens",
                ),
            }

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


class OpenAIEmbedder(OpenAICompatibleEmbedder):
    """Legacy OpenAI provider pinned to the official API endpoint."""

    _DEFAULT_BASE_URL = "https://api.openai.com/v1"
    _DEFAULT_MODEL = "text-embedding-3-small"
    _DEFAULT_DIM = 1536
    _PROVIDER_IDENTITY = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        model_revision: str | None = None,
        dim: int | None = None,
        send_dimensions: bool | None = None,
        batch_size: int | None = None,
        max_input_bytes: int | None = None,
        max_total_input_bytes: int | None = None,
        max_request_bytes: int | None = None,
        total_timeout_seconds: float | None = None,
        client: object | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # This class owns the official OpenAI credential.  Do not allow the
        # generic provider's endpoint override to redirect that credential to a
        # different host; callers needing another compatible service must use
        # OpenAICompatibleEmbedder explicitly.
        if base_url is not None and not _is_official_openai_base_url(base_url):
            raise ValueError("openai_base_url_must_be_official")
        resolved_base_url = self._DEFAULT_BASE_URL
        resolved_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")
        super().__init__(
            api_key=resolved_key,
            base_url=resolved_base_url,
            model=model or os.getenv("EMBEDDER_MODEL", self._DEFAULT_MODEL),
            model_revision=model_revision,
            dim=dim,
            send_dimensions=send_dimensions,
            batch_size=batch_size,
            max_input_bytes=max_input_bytes,
            max_total_input_bytes=max_total_input_bytes,
            max_request_bytes=max_request_bytes,
            total_timeout_seconds=total_timeout_seconds,
            client=client,
            clock=clock,
        )


def _is_official_openai_base_url(value: object) -> bool:
    """Return whether ``value`` is exactly the official OpenAI API root."""

    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").rstrip(".").casefold() == "api.openai.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") == "/v1"
        and not parsed.query
        and not parsed.fragment
    )


class LocalSentenceEmbedder(Embedder):
    """Local sentence-transformers embedder — in-process, zero HTTP.

    Runs the embedding model directly in the Python process via
    sentence-transformers (ONNX-optimized).  No external service needed,
    no network round-trips, no API keys.

    Default model: BAAI/bge-large-zh-v1.5 (1024 dim, Chinese+English).
    Set EMBEDDER_LOCAL_MODEL to override.

    First invocation downloads the model from HuggingFace (~1.3 GB),
    subsequent calls hit the disk cache and return in <5 ms.
    """

    _DEFAULT_MODEL = "BAAI/bge-large-zh-v1.5"
    _DIM = 1024

    def __init__(
        self,
        model_name: str | None = None,
        *,
        expected_dim: int | None = None,
        allow_ollama_recovery: bool = True,
    ) -> None:
        endpoint_role = os.environ.get("PP_ENDPOINT_ROLE", "").strip()
        if endpoint_role and endpoint_role != "pp-compute-node":
            raise ValueError("inference_requires_compute_node")
        self._model_name = model_name or os.getenv("EMBEDDER_LOCAL_MODEL", self._DEFAULT_MODEL)
        if expected_dim is None and (
            "PP_EMBEDDING_DIM" in os.environ or "EMBEDDER_DIMENSION" in os.environ
        ):
            expected_dim = _embedding_schema_dimension()
        self._expected_dim = _optional_expected_dimension(expected_dim)
        self._allow_ollama_recovery = bool(allow_ollama_recovery)
        self._model = None  # lazy-init

    def _lazy_load(self):
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ModuleNotFoundError as exc:
            if exc.name != "sentence_transformers":
                raise
            raise RuntimeError("local_embedding_dependency_missing") from exc

        # Use HF mirror in China if set.
        self._model = SentenceTransformer(
            self._model_name,
            trust_remote_code=True,
            local_files_only=False,
        )

    def embed(self, text: str) -> list[float]:
        self._lazy_load()
        vector = self._model.encode(text, normalize_embeddings=True).tolist()
        return _validate_embedding_vector(vector, self._expected_dim)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._lazy_load()
        vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        vectors = vecs.tolist()
        if not isinstance(vectors, list):
            raise RuntimeError("embedding_response_schema_invalid")
        return [_validate_embedding_vector(vector, self._expected_dim) for vector in vectors]

    @property
    def supports_native_batch(self) -> bool:
        return True

    @property
    def dim(self) -> int:
        return self._expected_dim or self._DIM

    @property
    def model_name(self) -> str:
        return self._model_name


class FallbackEmbedder(Embedder):
    """Local zero-vector fallback when no embedding service is available.

    Returns a zero vector of configurable dimension. Downstream systems
    (ContextEngine._text_retrieval) use pure text matching (CJK bigrams /
    word split) which does not depend on vector similarity, so retrieval
    still works — just without semantic ranking.
    """

    def __init__(self, dim: int | None = None) -> None:
        if dim is None:
            dim = _embedding_schema_dimension()
        if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
            raise ValueError("embedding_dimension_invalid")
        self._dim = dim
        self._model = "fallback-zero"

    def embed(self, text: str) -> list[float]:
        return [0.0] * self._dim

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]

    @property
    def supports_native_batch(self) -> bool:
        return True

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def index_model_name(self) -> str:
        # Preserve the established default identity while separating custom
        # dimensions so derived index material cannot be mixed across schemas.
        return self._model if self._dim == 1024 else f"{self._model}|dim={self._dim}"


# Singleton embedder instance — shared across all callers to enable cache reuse
_embedder_singleton: Embedder | None = None
_embedder_lock = threading.Lock()


def reset_embedder() -> Embedder | None:
    """Clear the embedder singleton so the next call to get_embedder() re-probes.

    Use when: Ollama becomes available after a FallbackEmbedder lock-in,
    or after deploying a new embedding model.
    """
    global _embedder_singleton
    with _embedder_lock:
        previous = _embedder_singleton
        _embedder_singleton = None
    if previous is not None:
        previous.close()
    logging.getLogger("plastic-promise.embedder").info(
        "Embedder singleton reset — will re-probe on next get_embedder()"
    )
    return previous


def get_embedder(fallback_on_error: bool = True) -> Embedder:
    """Factory: returns embedder based on EMBEDDER_PROVIDER env var.

    Provider selection is explicit: ``openai-compatible`` (legacy facade
    default), ``ollama`` (compatibility-only), ``local``, the cloud alias
    ``openai``, or ``fallback``.  The production local path is llama.cpp in
    ``MemoryIndexNodeRuntime`` and does not use this factory.
    A provider failure may produce a zero-vector fallback when
    ``fallback_on_error`` is enabled; providers are never probed implicitly.

    When fallback_on_error=True and all providers are unreachable,
    returns a FallbackEmbedder (zero vectors) so retrieval degrades to
    pure text matching instead of crashing.

    All embedders are wrapped in CachedEmbedder for performance (unless
    EMBEDDER_CACHE_SIZE=0). The embedder is a singleton shared across all
    callers, enabling cross-request embedding cache reuse.
    """
    global _embedder_singleton
    if _embedder_singleton is not None:
        return _embedder_singleton

    # The canonical backend may expose a governed node route, but it is never
    # an inference execution plane.  Keep legacy callers alive with a stable
    # text-only fallback until they are migrated to MemoryIndexNodeRuntime.
    endpoint_role = os.environ.get("PP_ENDPOINT_ROLE", "").strip()
    if endpoint_role == "pp-server-backend":
        with _embedder_lock:
            if _embedder_singleton is None:
                _embedder_singleton = FallbackEmbedder(
                    dim=_embedding_schema_dimension(default=1024)
                )
            return _embedder_singleton
    if endpoint_role and endpoint_role != "pp-compute-node":
        raise ValueError("inference_requires_compute_node")

    with _embedder_lock:
        if _embedder_singleton is not None:
            return _embedder_singleton

        provider = os.getenv("EMBEDDER_PROVIDER", "openai-compatible").strip().casefold()
        supported_providers = {
            "ollama",
            "local",
            "openai",
            "openai-compatible",
            "cloud",
            "fallback",
        }
        if provider not in supported_providers:
            raise ValueError("embedding_provider_invalid")
        # The legacy official OpenAI provider historically defaulted to the
        # 1536-dimensional text-embedding-3-small contract.  Keep that
        # fallback schema when PP_EMBEDDING_DIM is omitted, while all other
        # providers retain the current 1024-dimensional default.  An explicit
        # PP_EMBEDDING_DIM always wins through _embedding_schema_dimension().
        schema_default = (
            getattr(OpenAIEmbedder, "_DEFAULT_DIM", 1536) if provider == "openai" else 1024
        )
        schema_dimension = _embedding_schema_dimension(default=schema_default)
        delegate: Embedder | None = None

        if provider == "openai":
            try:
                delegate = OpenAIEmbedder()
            except Exception:
                if not fallback_on_error:
                    raise
        elif provider in {"openai-compatible", "cloud"}:
            try:
                delegate = OpenAICompatibleEmbedder()
            except Exception:
                if not fallback_on_error:
                    raise
        elif provider == "ollama":
            try:
                delegate = OllamaEmbedder(expected_dim=schema_dimension)
            except Exception:
                if not fallback_on_error:
                    raise
        elif provider == "fallback":
            delegate = FallbackEmbedder(dim=schema_dimension)
        elif provider == "local":
            try:
                delegate = LocalSentenceEmbedder(
                    expected_dim=schema_dimension,
                    allow_ollama_recovery=False,
                )
            except Exception:
                if not fallback_on_error:
                    raise
        else:
            # The remaining values are the explicit cloud aliases handled above.
            # Keep this branch unreachable so adding a provider requires updating
            # the allow-list and its construction path together.
            raise AssertionError("unsupported embedding provider")

        if delegate is None:
            _embedder_singleton = FallbackEmbedder(dim=schema_dimension)
            delegate = _embedder_singleton

        if os.environ.get("PP_MEMORY_CHUNKING", "off").strip().casefold() == "structure-v1":
            delegate = StructureAwareEmbedder(delegate)

        # Wrap in cache unless explicitly disabled
        cache_size = int(os.environ.get("EMBEDDER_CACHE_SIZE", "256"))
        _embedder_singleton = CachedEmbedder(delegate) if cache_size > 0 else delegate
        return _embedder_singleton
