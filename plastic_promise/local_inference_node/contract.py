"""Stable, non-secret contract for a local heterogeneous inference node.

The node is deliberately an inference-only process.  Its public contract is
small enough for the server scheduler to validate, while all canonical memory,
task, lease, audit, and index-generation state remains on the governed server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from plastic_promise.core.structured_token_budget import (
    UNBOUNDED_STRUCTURED_TOKEN_LIMIT,
    validate_structured_token_limit,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

NODE_PROTOCOL_VERSION = "local-inference-node/v1"
_UNPINNED_REVISIONS = frozenset({"latest", "main", "master", "stable", "head"})
_IMMUTABLE_REVISION = re.compile(r"(?:[0-9a-f]{40}|sha256:[0-9a-f]{64})$")
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}$")
_NODE_ID = re.compile(r"[a-z][a-z0-9_.:-]{1,127}$")


class NodeConfigurationError(ValueError):
    """Raised when the local node configuration violates its public contract."""


class EmbeddingEngine(Protocol):
    """The node's narrow, synchronous embedding seam."""

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return exactly one numeric vector for each input text."""


@dataclass(frozen=True)
class EmbeddingProviderIdentity:
    """Exact identity required for any local/cloud embedding fallback."""

    model: str
    revision: str
    dimension: int
    normalization: str

    def __post_init__(self) -> None:
        _require_safe_identity_string(self.model, "embedding_identity_model_invalid")
        _require_pinned_revision(self.revision, "embedding_identity_revision_invalid")
        if (
            not isinstance(self.dimension, int)
            or isinstance(self.dimension, bool)
            or self.dimension <= 0
        ):
            raise NodeConfigurationError("embedding_identity_dimension_invalid")
        if self.normalization not in {"l2", "none"}:
            raise NodeConfigurationError("embedding_identity_normalization_invalid")


class RerankingEngine(Protocol):
    """The node's narrow reranking seam.

    Implementations must return every candidate exactly once.  ``top_k`` is a
    scheduler hint; the node still returns a complete score vector so the
    governed caller can apply its own stable ordering and audit the result.
    """

    def rerank_tuples(
        self,
        query: str,
        candidates: Sequence[tuple[int, str]],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        """Return ``(candidate_index, score)`` for every candidate."""


class StructuredJSONEngine(Protocol):
    """The node's narrow structured-inference seam."""

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, object],
        max_tokens: int,
    ) -> dict[str, object]:
        """Return one bounded JSON object derived from the supplied payload."""


@dataclass(frozen=True)
class NodeIdentity:
    protocol_version: str
    node_id: str
    embedding_model: str
    embedding_revision: str
    embedding_dimension: int
    embedding_normalization: str
    rerank_model: str
    rerank_revision: str
    provider_class: str = "local"
    structured_json_model: str | None = None
    structured_json_revision: str | None = None
    embedding_artifact_sha256: str | None = None
    rerank_artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_safe_identity_string(self.protocol_version, "node_protocol_version")
        _require_safe_identity_string(self.node_id, "node_id")
        if _NODE_ID.fullmatch(self.node_id) is None:
            raise NodeConfigurationError("node_id_invalid")
        _require_safe_identity_string(self.embedding_model, "embedding_model")
        _require_pinned_revision(self.embedding_revision, "embedding_revision")
        _require_safe_identity_string(self.embedding_normalization, "embedding_normalization")
        _require_safe_identity_string(self.rerank_model, "rerank_model")
        _require_pinned_revision(self.rerank_revision, "rerank_revision")
        if self.provider_class not in {"local", "cloud", "hybrid"}:
            raise NodeConfigurationError("node_provider_class_invalid")
        if (self.structured_json_model is None) != (self.structured_json_revision is None):
            raise NodeConfigurationError("node_structured_json_identity_incomplete")
        if self.structured_json_model is not None:
            _require_safe_identity_string(
                self.structured_json_model,
                "structured_json_model",
            )
            _require_pinned_revision(
                self.structured_json_revision,
                "structured_json_revision",
            )
        if self.protocol_version != NODE_PROTOCOL_VERSION:
            raise NodeConfigurationError("node_protocol_version_unsupported")
        if (
            not isinstance(self.embedding_dimension, int)
            or isinstance(self.embedding_dimension, bool)
            or not 1 <= self.embedding_dimension <= 65_536
        ):
            raise NodeConfigurationError("node_embedding_dimension_invalid")
        if self.embedding_artifact_sha256 is not None:
            _require_sha256_digest(
                self.embedding_artifact_sha256, "node_embedding_artifact_sha256_invalid"
            )
        if self.rerank_artifact_sha256 is not None:
            _require_sha256_digest(
                self.rerank_artifact_sha256, "node_rerank_artifact_sha256_invalid"
            )

    def public_json(self) -> dict[str, object]:
        embedding: dict[str, object] = {
            "model": self.embedding_model,
            "revision": self.embedding_revision,
            "dimension": self.embedding_dimension,
            "normalization": self.embedding_normalization,
        }
        rerank: dict[str, object] = {
            "model": self.rerank_model,
            "revision": self.rerank_revision,
        }
        if self.embedding_artifact_sha256 is not None:
            embedding["artifact_sha256"] = self.embedding_artifact_sha256
        if self.rerank_artifact_sha256 is not None:
            rerank["artifact_sha256"] = self.rerank_artifact_sha256
        capabilities = ["embeddings", "rerank"]
        public: dict[str, object] = {
            "protocol_version": self.protocol_version,
            "node_id": self.node_id,
            "provider_class": self.provider_class,
            "capabilities": capabilities,
            "embedding": embedding,
            "rerank": rerank,
        }
        if self.structured_json_model is not None:
            capabilities.append("structured-json")
            public["structured_json"] = {
                "model": self.structured_json_model,
                "revision": self.structured_json_revision,
            }
        return public


@dataclass(frozen=True)
class NodeLimits:
    """Request limits applied before inference reaches a local model."""

    max_request_bytes: int = 1 * 1024 * 1024
    max_embedding_inputs: int = 64
    max_embedding_input_chars: int = 12_000
    max_rerank_documents: int = 128
    max_rerank_query_chars: int = 4_000
    max_rerank_document_chars: int = 12_000
    max_structured_system_prompt_bytes: int = 32 * 1024
    max_structured_user_payload_bytes: int = 256 * 1024
    max_structured_output_bytes: int = 256 * 1024
    # 0 means that the node does not impose an additional token ceiling.  The
    # request must still be a positive integer and remains bounded by output
    # bytes, provider policy, timeout, retry, and queue safeguards.
    max_structured_tokens: int = UNBOUNDED_STRUCTURED_TOKEN_LIMIT

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (
                self.max_request_bytes,
                self.max_embedding_inputs,
                self.max_embedding_input_chars,
                self.max_rerank_documents,
                self.max_rerank_query_chars,
                self.max_rerank_document_chars,
                self.max_structured_system_prompt_bytes,
                self.max_structured_user_payload_bytes,
                self.max_structured_output_bytes,
            )
        ):
            raise NodeConfigurationError("node_limits_must_be_positive")
        try:
            validate_structured_token_limit(self.max_structured_tokens)
        except ValueError as exc:
            raise NodeConfigurationError("node_structured_token_limit_invalid") from exc


def _require_safe_identity_string(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise NodeConfigurationError(f"{field}_invalid")
    if any(character.isspace() for character in value):
        raise NodeConfigurationError(f"{field}_invalid")


def _require_pinned_revision(value: str, field: str) -> None:
    _require_safe_identity_string(value, field)
    normalized = value.casefold()
    if normalized in _UNPINNED_REVISIONS or _IMMUTABLE_REVISION.fullmatch(normalized) is None:
        raise NodeConfigurationError(f"{field}_must_be_pinned")


def _require_sha256_digest(value: str | None, field: str) -> None:
    if not isinstance(value, str) or _SHA256_DIGEST.fullmatch(value) is None:
        raise NodeConfigurationError(field)
