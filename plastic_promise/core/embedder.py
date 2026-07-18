"""Plastic Promise Embedder — text-to-vector with provider abstraction.

Default: local sentence-transformers (BAAI/bge-large-zh-v1.5, 1024 dim).
Falls back to Ollama if local model unavailable.
Set EMBEDDER_PROVIDER=openai to use OpenAI text-embedding-3-small (1536 dim).

Provider chain (auto-detected):
  1. local   — sentence-transformers, in-process, zero HTTP (default)
  2. ollama  — local HTTP server, mxbai-embed-large (fallback)
  3. openai  — cloud API, text-embedding-3-small
  4. fallback — zero vectors, text-only retrieval (last resort)

Environment variables:
  EMBEDDER_PROVIDER=local|ollama|openai  (default: local)
  EMBEDDER_MODEL=mxbai-embed-large  (ollama model name)
  EMBEDDER_LOCAL_MODEL=BAAI/bge-large-zh-v1.5  (sentence-transformers model)
  OLLAMA_HOST=http://localhost:11434
  EMBEDDER_CACHE_SIZE=256          (default: 256, set to 0 to disable)
  EMBEDDER_CACHE_TTL=300           (TTL in seconds, default: 300)
  EMBEDDER_TIMEOUT=5               (HTTP timeout for Ollama/OpenAI, default: 5)
  PP_MEMORY_CHUNKING=off|shadow|structure-v1  (default: off)
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

import requests

from plastic_promise.core.chunking import (
    has_uncovered_content,
    legacy_character_chunks,
    limit_chunk_materials,
    shadow_chunking_diagnostics,
    structure_aware_chunks,
)


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
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

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
        if vec and not any(v != 0.0 for v in vec) and not isinstance(
            self._delegate, FallbackEmbedder
        ):
                import logging

                _log = logging.getLogger("plastic-promise.embedder")
                _log.warning(
                    "CachedEmbedder: delegate %s returned zero vector, "
                    "attempting runtime fallback to Ollama",
                    type(self._delegate).__name__,
                )
                try:
                    ollama_vec = OllamaEmbedder().embed(text)
                    if ollama_vec and any(v != 0.0 for v in ollama_vec):
                        _log.info(
                            "CachedEmbedder: Ollama runtime fallback succeeded, "
                            "switching delegate permanently"
                        )
                        self._delegate = OllamaEmbedder()
                        vec = ollama_vec
                except Exception as e:
                    _log.warning("CachedEmbedder: Ollama runtime fallback also failed: %s", e)

        with self._lock:
            if len(self._cache) >= self._max_size:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
                del self._cache[oldest_key]
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
        uncached_texts = []
        uncached_indices = []
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
            uncached_texts.append(text)
            uncached_indices.append(i)
            self._misses += 1

        if uncached_texts:
            new_vecs = self._delegate.embed_batch(uncached_texts)
            with self._lock:
                for j, vec in zip(uncached_indices, new_vecs, strict=True):
                    key = self._key(texts[j])
                    if len(self._cache) >= self._max_size:
                        oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
                        del self._cache[oldest_key]
                    self._cache[key] = (vec, now)
                    results.append((j, vec))

        results.sort(key=lambda x: x[0])
        return [v for _, v in results]

    @property
    def dim(self) -> int:
        return self._delegate.dim

    @property
    def model_name(self) -> str:
        return self._delegate.model_name

    @property
    def index_model_name(self) -> str:
        return self._delegate.index_model_name

    @property
    def last_chunking_diagnostics(self) -> dict[str, object]:
        diagnostics = getattr(self._delegate, "last_chunking_diagnostics", None)
        return dict(diagnostics) if isinstance(diagnostics, dict) else {}

    @property
    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
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

    def __init__(self, host: str | None = None, model: str | None = None) -> None:
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
            "EMBEDDER_STRUCTURE_HARD_CHARS", max(self._chunk_chars * 2, self._chunk_chars), minimum=1
        )
        self._structure_max_chunks = _int_env(
            "EMBEDDER_STRUCTURE_MAX_CHUNKS", 64, minimum=1
        )
        self._structure_max_source_chars = _int_env(
            "EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS", 2_000_000, minimum=1
        )
        self._last_chunking_diagnostics: dict[str, object] = {}

    def embed(self, text: str) -> list[float]:
        if self._chunking_mode == "structure-v1":
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

    @property
    def last_chunking_diagnostics(self) -> dict[str, object]:
        return dict(self._last_chunking_diagnostics)

    def _embed_chunk(self, text: str) -> list[float]:
        resp = requests.post(
            f"{self._host}/api/embeddings",
            json={"model": self._model, "prompt": text},
            timeout=float(os.getenv("EMBEDDER_TIMEOUT", "5")),
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    @property
    def dim(self) -> int:
        return 1024

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def index_model_name(self) -> str:
        if self._chunking_mode != "structure-v1":
            return self._model
        return (
            f"{self._model}|chunking=structure-v1"
            f"|target_chars={self._chunk_chars}"
            f"|hard_chars={self._structure_hard_chars}"
            f"|max_chunks={self._structure_max_chunks}"
            f"|max_source_chars={self._structure_max_source_chars}"
            "|budget=characters-fallback"
        )


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), minimum)
    except (TypeError, ValueError):
        return max(default, minimum)


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


class OpenAIEmbedder(Embedder):
    """OpenAI embedding fallback.

    Default model: text-embedding-3-small (1536 dim).
    Requires: pip install openai and OPENAI_API_KEY set.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._model = model or os.getenv("EMBEDDER_MODEL", "text-embedding-3-small")

    def embed(self, text: str) -> list[float]:
        from openai import OpenAI

        client = OpenAI(api_key=self._key)
        resp = client.embeddings.create(model=self._model, input=text)
        return resp.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(api_key=self._key)
        resp = client.embeddings.create(model=self._model, input=texts)
        return [d.embedding for d in resp.data]

    @property
    def dim(self) -> int:
        return 1536

    @property
    def model_name(self) -> str:
        return self._model


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

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or os.getenv("EMBEDDER_LOCAL_MODEL", self._DEFAULT_MODEL)
        self._model = None  # lazy-init

    def _lazy_load(self):
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer

        # Use HF mirror in China if set.
        self._model = SentenceTransformer(
            self._model_name,
            trust_remote_code=True,
            local_files_only=False,
        )

    def embed(self, text: str) -> list[float]:
        self._lazy_load()
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._lazy_load()
        vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return vecs.tolist()

    @property
    def dim(self) -> int:
        return self._DIM

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

    def __init__(self, dim: int = 1024) -> None:
        self._dim = dim
        self._model = "fallback-zero"

    def embed(self, text: str) -> list[float]:
        return [0.0] * self._dim

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model


# Singleton embedder instance — shared across all callers to enable cache reuse
_embedder_singleton: Embedder | None = None
_embedder_lock = threading.Lock()


def reset_embedder():
    """Clear the embedder singleton so the next call to get_embedder() re-probes.

    Use when: Ollama becomes available after a FallbackEmbedder lock-in,
    or after deploying a new embedding model.
    """
    global _embedder_singleton
    with _embedder_lock:
        _embedder_singleton = None
    logging.getLogger("plastic-promise.embedder").info(
        "Embedder singleton reset — will re-probe on next get_embedder()"
    )


def get_embedder(fallback_on_error: bool = True) -> Embedder:
    """Factory: returns embedder based on EMBEDDER_PROVIDER env var.

    Provider chain (auto-detected):
      1. ollama — local HTTP server (default, lightweight, 0.7GB mxbai-embed-large)
      2. local  — sentence-transformers, in-process (fallback if ollama fails)
      3. openai — cloud API
      4. fallback — zero vectors, text-only retrieval

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

    with _embedder_lock:
        if _embedder_singleton is not None:
            return _embedder_singleton

        provider = os.getenv("EMBEDDER_PROVIDER", "ollama").lower()
        delegate: Embedder | None = None

        if provider == "openai":
            try:
                delegate = OpenAIEmbedder()
            except Exception:
                if not fallback_on_error:
                    raise
        elif provider == "ollama":
            try:
                delegate = OllamaEmbedder()
            except Exception:
                if not fallback_on_error:
                    raise
        elif provider == "fallback":
            delegate = FallbackEmbedder(dim=1024)
        else:
            # "local" (default): try Ollama first (lightweight, 0.7GB mxbai-embed-large),
            # fall back to sentence-transformers BAAI/bge-large-zh-v1.5 (3.7GB) if Ollama unavailable
            try:
                delegate = OllamaEmbedder()
            except Exception as e:
                logging.info("OllamaEmbedder unavailable (%s), trying local model...", e)
                try:
                    delegate = LocalSentenceEmbedder()
                except Exception:
                    if not fallback_on_error:
                        raise

        if delegate is None:
            _embedder_singleton = FallbackEmbedder(dim=1024)
            return _embedder_singleton

        # Wrap in cache unless explicitly disabled
        cache_size = int(os.environ.get("EMBEDDER_CACHE_SIZE", "256"))
        _embedder_singleton = CachedEmbedder(delegate) if cache_size > 0 else delegate
        return _embedder_singleton
