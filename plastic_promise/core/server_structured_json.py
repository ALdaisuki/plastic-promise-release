"""Server-safe seam for structured JSON inference.

The concrete OpenAI-compatible transport is a compute-node concern and is
removed from the ``pp-server-backend`` artifact.  Common runtime modules may
still need to construct a semantic worker, so this module provides a bounded
unavailable implementation instead of importing the excluded provider.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping


class StructuredJSONProvider(Protocol):
    """Backend-neutral contract for normalized structured JSON inference."""

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
        max_tokens: int = 768,
    ) -> dict[str, object]: ...

    @property
    def identity(self) -> str: ...

    @property
    def stats(self) -> Mapping[str, object]: ...

    def close(self) -> None: ...


class UnavailableStructuredJSONProvider:
    """Stable fail-closed provider used when the server owns no inference."""

    identity = "structured-json:unavailable"

    @property
    def stats(self) -> Mapping[str, object]:
        return {"state": "unavailable", "reason": "structured_json_provider_unavailable"}

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
        max_tokens: int = 768,
    ) -> dict[str, object]:
        raise RuntimeError("structured_json_provider_unavailable")

    def close(self) -> None:
        return None


def _server_role() -> bool:
    return os.environ.get("PP_ENDPOINT_ROLE", "").strip() == "pp-server-backend"


def create_structured_json_provider(**kwargs: Any) -> StructuredJSONProvider:
    """Create a compute provider outside the server, or fail closed on server."""

    if _server_role():
        return UnavailableStructuredJSONProvider()
    try:
        provider_module = importlib.import_module("plastic_promise.core." + "inference_provider")
        provider_type = provider_module.OpenAICompatibleJSONProvider
    except (AttributeError, ImportError, ModuleNotFoundError):
        return UnavailableStructuredJSONProvider()
    return provider_type(**kwargs)


class OpenAICompatibleJSONProvider:
    """Compatibility constructor that resolves the concrete provider lazily."""

    def __new__(cls, **kwargs: Any) -> StructuredJSONProvider:
        return create_structured_json_provider(**kwargs)
