"""Server-safe embedding contract and compatibility adapter.

The canonical backend may need an embedding-shaped object for LanceDB and
legacy text-only paths, but it is not an inference execution plane.  This
module therefore exposes only the neutral contract plus a zero-vector
fallback.  In a developer process (or a compute process) it may delegate to
the legacy provider module for compatibility; the server image deliberately
excludes that provider module, so the same code fails closed to the fallback.
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from typing import Any


class _NeutralEmbedder(ABC):
    """Small provider-neutral interface consumed by server-owned modules."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return one vector for *text*."""

    async def aembed(self, text: str) -> list[float]:
        return await asyncio.to_thread(self.embed, text)

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return vectors in input order."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Vector dimension."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Safe model identity."""

    @property
    def index_model_name(self) -> str:
        return self.model_name

    @property
    def supports_native_batch(self) -> bool:
        return False

    def prepare_index_text(self, text: str) -> str:
        return text

    def close(self) -> None:
        return None


class _NeutralFallbackEmbedder(_NeutralEmbedder):
    """Text-only retrieval fallback with no provider or network behavior."""

    def __init__(self, dim: int | None = None) -> None:
        raw_dim = dim if dim is not None else os.environ.get("PP_EMBEDDING_DIM", "1024")
        try:
            resolved_dim = int(raw_dim)
        except (TypeError, ValueError) as exc:
            raise ValueError("embedding_dimension_invalid") from exc
        if resolved_dim <= 0:
            raise ValueError("embedding_dimension_invalid")
        self._dim = resolved_dim

    def embed(self, text: str) -> list[float]:
        del text
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
        return "fallback-zero"

    @property
    def index_model_name(self) -> str:
        return "fallback-zero" if self._dim == 1024 else f"fallback-zero|dim={self._dim}"


_provider_module: Any | None = None
if os.environ.get("PP_ENDPOINT_ROLE", "").strip() != "pp-server-backend":
    try:
        from plastic_promise.core import embedder as _provider_module
    except (ImportError, ModuleNotFoundError):
        _provider_module = None


Embedder = getattr(_provider_module, "Embedder", _NeutralEmbedder)
FallbackEmbedder = getattr(_provider_module, "FallbackEmbedder", _NeutralFallbackEmbedder)


def get_embedder(*, fallback_on_error: bool = True) -> Embedder:
    """Return a governed provider in development/compute, fallback on server."""

    if _provider_module is not None and os.environ.get("PP_ENDPOINT_ROLE", "").strip() != "pp-server-backend":
        return _provider_module.get_embedder(fallback_on_error=fallback_on_error)
    return FallbackEmbedder()


def reset_embedder() -> object | None:
    """Reset a delegated provider; server fallback has no mutable state."""

    if _provider_module is not None and os.environ.get("PP_ENDPOINT_ROLE", "").strip() != "pp-server-backend":
        return _provider_module.reset_embedder()
    return None
