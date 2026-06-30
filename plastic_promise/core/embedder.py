"""Plastic Promise Embedder — text-to-vector with provider abstraction.

Default: Ollama with mxbai-embed-large (1024 dim).
Set EMBEDDER_PROVIDER=openai to use OpenAI text-embedding-3-small (1536 dim).

Environment variables:
  EMBEDDER_PROVIDER=ollama|openai  (default: ollama)
  OLLAMA_HOST=http://localhost:11434
  EMBEDDER_MODEL=mxbai-embed-large
  EMBEDDER_CACHE_SIZE=256          (default: 256, set to 0 to disable)
  EMBEDDER_CACHE_TTL=300           (TTL in seconds, default: 300)
"""

import hashlib
import os
import threading
import time
from abc import ABC, abstractmethod

import requests


class Embedder(ABC):
    """Abstract text-to-vector embedder."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Convert text to an embedding vector."""

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


class CachedEmbedder(Embedder):
    """LRU embedding cache wrapper — eliminates redundant Ollama/API calls.

    Caches embeddings by content hash. Thread-safe with TTL-based expiry.
    Configurable via EMBEDDER_CACHE_SIZE (default 256) and EMBEDDER_CACHE_TTL (default 300s).
    Set EMBEDDER_CACHE_SIZE=0 to disable.
    """

    def __init__(self, delegate: Embedder,
                 max_size: int = None,
                 ttl_seconds: int = None) -> None:
        self._delegate = delegate
        self._max_size = max_size if max_size is not None else int(
            os.environ.get("EMBEDDER_CACHE_SIZE", "256"))
        self._ttl = ttl_seconds if ttl_seconds is not None else int(
            os.environ.get("EMBEDDER_CACHE_TTL", "300"))
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
        with self._lock:
            if len(self._cache) >= self._max_size:
                # Evict oldest entry
                oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
                del self._cache[oldest_key]
            self._cache[key] = (vec, now)
        return vec

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
                for j, vec in zip(uncached_indices, new_vecs):
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

    def embed(self, text: str) -> list[float]:
        resp = requests.post(
            f"{self._host}/api/embeddings",
            json={"model": self._model, "prompt": text},
            timeout=30,
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


def get_embedder(fallback_on_error: bool = True) -> Embedder:
    """Factory: returns embedder based on EMBEDDER_PROVIDER env var.

    When fallback_on_error=True and the primary embedder is unreachable,
    returns a FallbackEmbedder (zero vectors) so retrieval degrades to
    pure text matching instead of crashing.

    All embedders are wrapped in CachedEmbedder for performance (unless
    EMBEDDER_CACHE_SIZE=0). The embedder is a singleton shared across all
    callers, enabling cross-request embedding cache reuse.

    Returns:
        OllamaEmbedder by default (mxbai-embed-large), wrapped in cache.
        OpenAIEmbedder if EMBEDDER_PROVIDER=openai, wrapped in cache.
        FallbackEmbedder if primary is unreachable and fallback_on_error=True.
    """
    global _embedder_singleton
    if _embedder_singleton is not None:
        return _embedder_singleton

    with _embedder_lock:
        if _embedder_singleton is not None:
            return _embedder_singleton

        provider = os.getenv("EMBEDDER_PROVIDER", "ollama").lower()

        if provider == "openai":
            try:
                delegate = OpenAIEmbedder()
            except Exception:
                if fallback_on_error:
                    _embedder_singleton = FallbackEmbedder(dim=1536)
                    return _embedder_singleton
                raise
        else:
            try:
                delegate = OllamaEmbedder()
            except Exception:
                if fallback_on_error:
                    _embedder_singleton = FallbackEmbedder(dim=1024)
                    return _embedder_singleton
                raise

        # Wrap in cache unless explicitly disabled
        cache_size = int(os.environ.get("EMBEDDER_CACHE_SIZE", "256"))
        if cache_size > 0:
            _embedder_singleton = CachedEmbedder(delegate)
        else:
            _embedder_singleton = delegate
        return _embedder_singleton
