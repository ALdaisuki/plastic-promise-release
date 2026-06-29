"""Plastic Promise Embedder — text-to-vector with provider abstraction.

Default: Ollama with mxbai-embed-large (1024 dim).
Set EMBEDDER_PROVIDER=openai to use OpenAI text-embedding-3-small (1536 dim).

Environment variables:
  EMBEDDER_PROVIDER=ollama|openai  (default: ollama)
  OLLAMA_HOST=http://localhost:11434
  EMBEDDER_MODEL=mxbai-embed-large
"""

import os
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


def get_embedder(fallback_on_error: bool = True) -> Embedder:
    """Factory: returns embedder based on EMBEDDER_PROVIDER env var.

    When fallback_on_error=True and the primary embedder is unreachable,
    returns a FallbackEmbedder (zero vectors) so retrieval degrades to
    pure text matching instead of crashing.

    Returns:
        OllamaEmbedder by default (mxbai-embed-large).
        OpenAIEmbedder if EMBEDDER_PROVIDER=openai.
        FallbackEmbedder if primary is unreachable and fallback_on_error=True.
    """
    provider = os.getenv("EMBEDDER_PROVIDER", "ollama").lower()

    if provider == "openai":
        try:
            return OpenAIEmbedder()
        except Exception:
            if fallback_on_error:
                return FallbackEmbedder(dim=1536)
            raise

    try:
        return OllamaEmbedder()
    except Exception:
        if fallback_on_error:
            return FallbackEmbedder(dim=1024)
        raise
