"""Backend-owned contracts for prepared embeddings and immutable reranking.

Frontend callers provide normalized content, never provider configuration or
credentials. Embeddings are optional: the backend fills missing vectors with
its process-configured cloud or local embedder. A supplied vector is accepted
only for request-scoped reuse when its declared model identity and exact
material hash match the active backend embedder. Those checks are not proof of
model provenance, so frontend vectors are never authorized for direct index
persistence.

Reranking is deliberately stateless and immutable here. Deployments that make
it asynchronous must add a durable job store keyed by project and idempotency
key, and must reject reuse of a key with a different ``input_hash``. The pure
request binding lets that check happen before any provider call.

A frontend-side local model can consume an exported package containing exact
text plus vector receipts, but never provider credentials or raw vectors. Its
reported ranking is accepted only while the authenticated project, request,
candidate version, candidate hash, and package hash still match current server
state. The accepted result remains request-scoped and has no index authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from plastic_promise.core.embedder import Embedder, get_embedder
from plastic_promise.core.reranker import MultiProviderReranker

PREPARED_INPUT_CONTRACT = "prepared-input/v1"
RERANK_REQUEST_CONTRACT = "rerank-request/v1"
RERANK_CONTRACT = "rerank/v1"
RERANK_SCORING_VERSION = "provider-blend/v1"
CLIENT_LOCAL_RERANK_CONTRACT = "client-local-rerank/v2"
CLIENT_LOCAL_RESULT_CONTRACT = "client-local-rerank-result/v1"
CLIENT_LOCAL_SCORING_VERSION = "client-local-score/v1"

_DEFAULT_MAX_ITEMS = 100
_HARD_MAX_ITEMS = 1_000
_DEFAULT_MAX_TEXT_BYTES = 64 * 1024
_HARD_MAX_TEXT_BYTES = 1024 * 1024
_DEFAULT_MAX_QUERY_BYTES = 16 * 1024
_HARD_MAX_QUERY_BYTES = 256 * 1024
_DEFAULT_MAX_CLIENT_LOCAL_PACKAGE_BYTES = 3 * 1024 * 1024
_HARD_MAX_CLIENT_LOCAL_PACKAGE_BYTES = 64 * 1024 * 1024
_MAX_IDENTIFIER_BYTES = 512
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ITEM_FIELDS = frozenset({"id", "text", "base_score", "embedding"})
_EMBEDDING_FIELDS = frozenset({"vector", "dimension", "identity", "material_sha256"})
_CLIENT_LOCAL_RESULT_FIELDS = frozenset(
    {"contract_version", "package_hash", "model_identity", "items"}
)
_CLIENT_LOCAL_RESULT_ITEM_FIELDS = frozenset({"id", "score"})


@dataclass(frozen=True)
class ProvidedEmbedding:
    """A request-scoped vector supplied with an exact material receipt."""

    vector: tuple[float, ...]
    dimension: int
    identity: str
    material_sha256: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ProvidedEmbedding:
        _require_mapping_fields(payload, _EMBEDDING_FIELDS)
        dimension = _positive_int(payload.get("dimension"), "provided_embedding_dimension_invalid")
        vector = _validated_vector(
            payload.get("vector"),
            dimension,
            prefix="provided_embedding",
        )
        identity = _bounded_identifier(
            payload.get("identity"),
            reason="provided_embedding_identity_invalid",
        )
        material_sha256 = payload.get("material_sha256")
        if not isinstance(material_sha256, str) or not _SHA256_RE.fullmatch(material_sha256):
            raise ValueError("provided_embedding_material_hash_invalid")
        return cls(
            vector=vector,
            dimension=dimension,
            identity=identity,
            material_sha256=material_sha256,
        )


@dataclass(frozen=True)
class IntegratedInput:
    """Normalized frontend input with an optional embedding."""

    item_id: str
    text: str
    base_score: float
    embedding: ProvidedEmbedding | None = None

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        max_text_bytes: int = _DEFAULT_MAX_TEXT_BYTES,
    ) -> IntegratedInput:
        _require_mapping_fields(payload, _ITEM_FIELDS)
        item_id = _bounded_identifier(payload.get("id"), reason="input_id_invalid")
        text = _bounded_text(payload.get("text"), max_text_bytes=max_text_bytes)
        base_score = _bounded_score(payload.get("base_score", 0.0))
        raw_embedding = payload.get("embedding")
        if raw_embedding is None:
            embedding = None
        elif isinstance(raw_embedding, Mapping):
            embedding = ProvidedEmbedding.from_mapping(raw_embedding)
        else:
            raise TypeError("provided_embedding_mapping_required")
        return cls(
            item_id=item_id,
            text=text,
            base_score=base_score,
            embedding=embedding,
        )


@dataclass(frozen=True)
class PreparedInput:
    """Validated request material ready for backend inference."""

    item_id: str
    text: str
    base_score: float
    embedding: tuple[float, ...]
    embedding_identity: str
    material_sha256: str
    embedding_provenance: str
    reusable_for_index: bool = False


@dataclass(frozen=True)
class PreparedBatch:
    contract_version: str
    embedding_identity: str
    embedding_dimension: int
    items: tuple[PreparedInput, ...]
    provided_count: int
    generated_count: int


@dataclass(frozen=True)
class RankedInput:
    item_id: str
    score: float
    rank: int


@dataclass(frozen=True)
class RerankResult:
    """Version-bound result; clients must reject it after candidate drift."""

    contract_version: str
    scoring_version: str
    project_id: str
    request_id: str
    idempotency_key_hash: str
    candidate_set_version: str
    candidate_set_hash: str
    query_hash: str
    input_hash: str
    provider_policy_revision: str
    model_identity: str
    top_k: int
    items: tuple[RankedInput, ...]
    diagnostics: Mapping[str, object]


@dataclass(frozen=True)
class RerankRequestBinding:
    """Pure preflight binding used by a durable idempotency/job store."""

    contract_version: str
    scoring_version: str
    project_id: str
    request_id: str
    idempotency_key_hash: str
    candidate_set_version: str
    candidate_set_hash: str
    query_hash: str
    input_hash: str
    provider_policy_revision: str
    top_k: int


@dataclass(frozen=True)
class ClientLocalCandidate:
    """Exact text plus receipts exported for request-scoped local reranking."""

    item_id: str
    text: str
    base_score: float
    material_sha256: str
    embedding_sha256: str


@dataclass(frozen=True)
class ClientLocalRerankPackage:
    """Immutable package a frontend-side local model may consume."""

    contract_version: str
    scoring_version: str
    project_id: str
    request_id: str
    candidate_set_version: str
    candidate_set_hash: str
    query: str
    query_hash: str
    embedding_identity: str
    embedding_dimension: int
    model_identity: str
    top_k: int
    candidates: tuple[ClientLocalCandidate, ...]
    package_hash: str


@dataclass(frozen=True)
class ClientLocalRerankResult:
    """Validated local result that remains scoped to one immutable package."""

    contract_version: str
    scoring_version: str
    project_id: str
    request_id: str
    package_hash: str
    candidate_set_version: str
    candidate_set_hash: str
    query_hash: str
    reported_model_identity: str
    top_k: int
    items: tuple[RankedInput, ...]


class BackendInferenceService:
    """Prepare and rerank integrated data using backend-selected providers."""

    def __init__(
        self,
        *,
        embedder: Embedder | None,
        reranker_factory: Callable[[], object] = MultiProviderReranker,
        provider_policy_revision: str | None = None,
        max_items: int = _DEFAULT_MAX_ITEMS,
        max_text_bytes: int = _DEFAULT_MAX_TEXT_BYTES,
        max_query_bytes: int = _DEFAULT_MAX_QUERY_BYTES,
    ) -> None:
        self._embedder = embedder
        self._reranker_factory = reranker_factory
        self._provider_policy_revision = _bounded_identifier(
            provider_policy_revision or _runtime_rerank_policy_revision(),
            reason="provider_policy_revision_invalid",
        )
        self._max_items = _bounded_limit(
            max_items,
            default=_DEFAULT_MAX_ITEMS,
            maximum=_HARD_MAX_ITEMS,
            reason="input_max_items_invalid",
        )
        self._max_text_bytes = _bounded_limit(
            max_text_bytes,
            default=_DEFAULT_MAX_TEXT_BYTES,
            maximum=_HARD_MAX_TEXT_BYTES,
            reason="input_max_text_bytes_invalid",
        )
        self._max_query_bytes = _bounded_limit(
            max_query_bytes,
            default=_DEFAULT_MAX_QUERY_BYTES,
            maximum=_HARD_MAX_QUERY_BYTES,
            reason="rerank_max_query_bytes_invalid",
        )

    @classmethod
    def from_runtime(cls, **kwargs: object) -> BackendInferenceService:
        """Use the server's configured cloud or local provider policy."""

        return cls(embedder=get_embedder(fallback_on_error=False), **kwargs)

    @classmethod
    def from_rerank_runtime(cls, **kwargs: object) -> BackendInferenceService:
        """Construct the provider service without initializing an embedder.

        Durable cloud jobs already contain an authoritative, validated package,
        so retry and startup recovery need only the reranker.  Keeping this a
        public factory avoids private-attribute probing and prevents an
        embedding outage from blocking work that no longer needs embeddings.
        """

        return cls(embedder=None, **kwargs)

    @property
    def provider_policy_revision(self) -> str:
        return self._provider_policy_revision

    @property
    def embedding_identity(self) -> str:
        """Return the exact index identity expected for supplied vectors."""

        return _bounded_identifier(
            self._require_embedder().index_model_name,
            reason="embedding_identity_invalid",
        )

    @property
    def embedding_dimension(self) -> int:
        """Return the active backend embedding dimension."""

        return _positive_int(self._require_embedder().dim, "embedding_dimension_invalid")

    def _require_embedder(self) -> Embedder:
        if self._embedder is None:
            raise RuntimeError("backend_embedding_service_unavailable")
        return self._embedder

    def prepare(self, payloads: Sequence[Mapping[str, object]]) -> PreparedBatch:
        items = self._parse_items(payloads)
        embedder = self._require_embedder()
        expected_dim = self.embedding_dimension
        expected_identity = self.embedding_identity
        prepared: list[PreparedInput | None] = [None] * len(items)
        missing: list[tuple[int, IntegratedInput]] = []
        provided_count = 0

        for index, item in enumerate(items):
            material_hash = material_sha256(item.text)
            supplied = item.embedding
            if supplied is None:
                missing.append((index, item))
                continue
            if supplied.dimension != expected_dim:
                raise ValueError("provided_embedding_dimension_mismatch")
            if supplied.identity != expected_identity:
                raise ValueError("provided_embedding_identity_mismatch")
            if supplied.material_sha256 != material_hash:
                raise ValueError("provided_embedding_material_hash_mismatch")
            vector = _validated_vector(
                supplied.vector,
                expected_dim,
                prefix="provided_embedding",
            )
            prepared[index] = PreparedInput(
                item_id=item.item_id,
                text=item.text,
                base_score=item.base_score,
                embedding=vector,
                embedding_identity=expected_identity,
                material_sha256=material_hash,
                embedding_provenance="frontend-supplied",
            )
            provided_count += 1

        if missing:
            generated = embedder.embed_batch([item.text for _, item in missing])
            if not isinstance(generated, list) or len(generated) != len(missing):
                raise RuntimeError("backend_embedding_count_mismatch")
            for (index, item), raw_vector in zip(missing, generated, strict=True):
                vector = _validated_vector(
                    raw_vector,
                    expected_dim,
                    prefix="backend_embedding",
                )
                prepared[index] = PreparedInput(
                    item_id=item.item_id,
                    text=item.text,
                    base_score=item.base_score,
                    embedding=vector,
                    embedding_identity=expected_identity,
                    material_sha256=material_sha256(item.text),
                    embedding_provenance="backend-generated",
                )

        if any(item is None for item in prepared):
            raise RuntimeError("backend_embedding_result_incomplete")
        resolved = tuple(item for item in prepared if item is not None)
        return PreparedBatch(
            contract_version=PREPARED_INPUT_CONTRACT,
            embedding_identity=expected_identity,
            embedding_dimension=expected_dim,
            items=resolved,
            provided_count=provided_count,
            generated_count=len(missing),
        )

    def validate_rerank_submission(
        self,
        *,
        payloads: Sequence[Mapping[str, object]],
        query: object,
        project_id: object,
        request_id: object,
        idempotency_key: object,
        candidate_set_version: object,
        top_k: object = None,
    ) -> None:
        """Validate all request metadata before any embedding provider call."""

        validate_rerank_submission(
            payloads=payloads,
            query=query,
            project_id=project_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            candidate_set_version=candidate_set_version,
            top_k=top_k,
            max_items=self._max_items,
            max_text_bytes=self._max_text_bytes,
            max_query_bytes=self._max_query_bytes,
        )

    async def aprepare(self, payloads: Sequence[Mapping[str, object]]) -> PreparedBatch:
        """Run blocking local/cloud embedding work outside an async event loop."""

        return await asyncio.to_thread(self.prepare, payloads)

    def bind_rerank_request(
        self,
        *,
        query: str,
        prepared: PreparedBatch,
        project_id: str,
        request_id: str,
        idempotency_key: str,
        candidate_set_version: str,
        top_k: int | None = None,
    ) -> RerankRequestBinding:
        """Validate and hash a rerank request before any provider call is made."""

        if not isinstance(prepared, PreparedBatch):
            raise TypeError("prepared_batch_required")
        if prepared.contract_version != PREPARED_INPUT_CONTRACT:
            raise ValueError("prepared_batch_contract_mismatch")
        query = _bounded_query(query, max_query_bytes=self._max_query_bytes)
        project_id = _bounded_identifier(project_id, reason="rerank_project_id_invalid")
        request_id = _bounded_identifier(request_id, reason="rerank_request_id_invalid")
        idempotency_key = _bounded_identifier(
            idempotency_key,
            reason="rerank_idempotency_key_invalid",
        )
        candidate_set_version = _bounded_identifier(
            candidate_set_version,
            reason="rerank_candidate_set_version_invalid",
        )
        resolved_top_k = _resolve_top_k(prepared, top_k, reason="rerank_top_k_invalid")

        query_hash = material_sha256(query)
        candidate_set_hash = _candidate_set_hash(prepared, candidate_set_version)
        idempotency_key_hash = _canonical_sha256(
            {
                "project_id": project_id,
                "idempotency_key": idempotency_key,
            }
        )
        input_hash = _canonical_sha256(
            {
                "project_id": project_id,
                "candidate_set_hash": candidate_set_hash,
                "query_hash": query_hash,
                "top_k": resolved_top_k,
                "provider_policy_revision": self._provider_policy_revision,
                "scoring_version": RERANK_SCORING_VERSION,
            }
        )
        return RerankRequestBinding(
            contract_version=RERANK_REQUEST_CONTRACT,
            scoring_version=RERANK_SCORING_VERSION,
            project_id=project_id,
            request_id=request_id,
            idempotency_key_hash=idempotency_key_hash,
            candidate_set_version=candidate_set_version,
            candidate_set_hash=candidate_set_hash,
            query_hash=query_hash,
            input_hash=input_hash,
            provider_policy_revision=self._provider_policy_revision,
            top_k=resolved_top_k,
        )

    def rerank(
        self,
        *,
        query: str,
        prepared: PreparedBatch,
        project_id: str,
        request_id: str,
        idempotency_key: str,
        candidate_set_version: str,
        top_k: int | None = None,
    ) -> RerankResult:
        binding = self.bind_rerank_request(
            query=query,
            prepared=prepared,
            project_id=project_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            candidate_set_version=candidate_set_version,
            top_k=top_k,
        )
        tuples = [(item.item_id, item.text, item.base_score) for item in prepared.items]
        reranker = self._reranker_factory()
        rerank_tuples = getattr(reranker, "rerank_tuples", None)
        if not callable(rerank_tuples):
            raise TypeError("reranker_contract_invalid")
        ranked = rerank_tuples(query, list(tuples), top_k=binding.top_k)
        ranked_items = _validated_ranked_items(ranked, prepared, binding.top_k)
        diagnostics = getattr(reranker, "last_diagnostics", {})
        if not isinstance(diagnostics, Mapping):
            diagnostics = {}
        model_identity = getattr(reranker, "last_model_identity", None)
        if not isinstance(model_identity, str) or not model_identity.strip():
            provider = diagnostics.get("provider", "unknown")
            model_identity = f"provider:{provider}"
        try:
            model_identity = _bounded_identifier(
                model_identity,
                reason="rerank_model_identity_invalid",
            )
        except ValueError:
            raise RuntimeError("rerank_model_identity_invalid") from None

        return RerankResult(
            contract_version=RERANK_CONTRACT,
            scoring_version=binding.scoring_version,
            project_id=binding.project_id,
            request_id=binding.request_id,
            idempotency_key_hash=binding.idempotency_key_hash,
            candidate_set_version=binding.candidate_set_version,
            candidate_set_hash=binding.candidate_set_hash,
            query_hash=binding.query_hash,
            input_hash=binding.input_hash,
            provider_policy_revision=binding.provider_policy_revision,
            model_identity=model_identity,
            top_k=binding.top_k,
            items=ranked_items,
            diagnostics=_freeze_mapping(diagnostics),
        )

    def rerank_authoritative_package(
        self,
        *,
        package: ClientLocalRerankPackage,
        binding: RerankRequestBinding,
    ) -> RerankResult:
        """Rerank a package persisted by the server's durable job store.

        A retry after a process restart must not depend on a client replaying
        vectors or on re-embedding the same text.  The package is the server
        authority for the immutable candidate text and binding; the cloud
        reranker only needs that text.  This method therefore validates the
        package/hash relationship and runs the provider without reconstructing
        a synthetic ``PreparedBatch``.
        """

        if not isinstance(package, ClientLocalRerankPackage):
            raise TypeError("client_local_package_required")
        if not isinstance(binding, RerankRequestBinding):
            raise TypeError("rerank_binding_required")
        _validate_client_local_package(
            package,
            max_items=self._max_items,
            max_package_bytes=_DEFAULT_MAX_CLIENT_LOCAL_PACKAGE_BYTES,
            max_query_bytes=self._max_query_bytes,
            max_text_bytes=self._max_text_bytes,
        )
        if binding.provider_policy_revision != self._provider_policy_revision:
            raise ValueError("rerank_provider_policy_revision_mismatch")
        if (
            binding.project_id != package.project_id
            or binding.request_id != package.request_id
            or binding.candidate_set_version != package.candidate_set_version
            or binding.candidate_set_hash != package.candidate_set_hash
            or binding.query_hash != package.query_hash
            or binding.top_k != package.top_k
            or binding.scoring_version != RERANK_SCORING_VERSION
        ):
            raise ValueError("rerank_binding_package_mismatch")

        tuples = [
            (candidate.item_id, candidate.text, candidate.base_score)
            for candidate in package.candidates
        ]
        reranker = self._reranker_factory()
        rerank_tuples = getattr(reranker, "rerank_tuples", None)
        if not callable(rerank_tuples):
            raise TypeError("reranker_contract_invalid")
        ranked = rerank_tuples(package.query, list(tuples), top_k=binding.top_k)
        ranked_items = _validated_authoritative_ranked_items(
            ranked,
            allowed_ids={candidate.item_id for candidate in package.candidates},
            top_k=binding.top_k,
        )
        diagnostics = getattr(reranker, "last_diagnostics", {})
        if not isinstance(diagnostics, Mapping):
            diagnostics = {}
        model_identity = getattr(reranker, "last_model_identity", None)
        if not isinstance(model_identity, str) or not model_identity.strip():
            model_identity = f"provider:{diagnostics.get('provider', 'unknown')}"
        model_identity = _bounded_identifier(
            model_identity,
            reason="rerank_model_identity_invalid",
        )
        return RerankResult(
            contract_version=RERANK_CONTRACT,
            scoring_version=binding.scoring_version,
            project_id=binding.project_id,
            request_id=binding.request_id,
            idempotency_key_hash=binding.idempotency_key_hash,
            candidate_set_version=binding.candidate_set_version,
            candidate_set_hash=binding.candidate_set_hash,
            query_hash=binding.query_hash,
            input_hash=binding.input_hash,
            provider_policy_revision=binding.provider_policy_revision,
            model_identity=model_identity,
            top_k=binding.top_k,
            items=ranked_items,
            diagnostics=_freeze_mapping(diagnostics),
        )

    async def arerank(
        self,
        *,
        query: str,
        prepared: PreparedBatch,
        project_id: str,
        request_id: str,
        idempotency_key: str,
        candidate_set_version: str,
        top_k: int | None = None,
    ) -> RerankResult:
        """Run blocking rerank provider work outside an async event loop."""

        return await asyncio.to_thread(
            self.rerank,
            query=query,
            prepared=prepared,
            project_id=project_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            candidate_set_version=candidate_set_version,
            top_k=top_k,
        )

    def export_client_local_rerank(
        self,
        *,
        query: str,
        prepared: PreparedBatch,
        authenticated_project_id: str,
        request_id: str,
        candidate_set_version: str,
        model_identity: str,
        top_k: int | None = None,
    ) -> ClientLocalRerankPackage:
        """Export exact request material without provider settings or vectors."""

        if not isinstance(prepared, PreparedBatch):
            raise TypeError("prepared_batch_required")
        if prepared.contract_version != PREPARED_INPUT_CONTRACT:
            raise ValueError("prepared_batch_contract_mismatch")
        query = _bounded_query(query, max_query_bytes=self._max_query_bytes)
        project_id = _bounded_identifier(
            authenticated_project_id,
            reason="client_local_project_id_invalid",
        )
        request_id = _bounded_identifier(
            request_id,
            reason="client_local_request_id_invalid",
        )
        candidate_set_version = _bounded_identifier(
            candidate_set_version,
            reason="client_local_candidate_set_version_invalid",
        )
        model_identity = _bounded_identifier(
            model_identity,
            reason="client_local_model_identity_invalid",
        )
        resolved_top_k = _resolve_top_k(prepared, top_k, reason="client_local_top_k_invalid")
        candidates = _client_local_candidates(prepared)
        candidate_set_hash = _candidate_set_hash(prepared, candidate_set_version)
        query_hash = material_sha256(query)
        package_material = _client_local_package_material(
            project_id=project_id,
            request_id=request_id,
            candidate_set_version=candidate_set_version,
            candidate_set_hash=candidate_set_hash,
            query=query,
            query_hash=query_hash,
            embedding_identity=prepared.embedding_identity,
            embedding_dimension=prepared.embedding_dimension,
            model_identity=model_identity,
            top_k=resolved_top_k,
            candidates=candidates,
        )
        return ClientLocalRerankPackage(
            contract_version=CLIENT_LOCAL_RERANK_CONTRACT,
            scoring_version=CLIENT_LOCAL_SCORING_VERSION,
            project_id=project_id,
            request_id=request_id,
            candidate_set_version=candidate_set_version,
            candidate_set_hash=candidate_set_hash,
            query=query,
            query_hash=query_hash,
            embedding_identity=prepared.embedding_identity,
            embedding_dimension=prepared.embedding_dimension,
            model_identity=model_identity,
            top_k=resolved_top_k,
            candidates=candidates,
            package_hash=_canonical_sha256(package_material),
        )

    def accept_client_local_rerank(
        self,
        *,
        package: ClientLocalRerankPackage,
        payload: Mapping[str, object],
        authenticated_project_id: str,
        current_request_id: str,
        current_query: str,
        current_candidate_set_version: str,
        current_prepared: PreparedBatch,
        current_top_k: int | None = None,
    ) -> ClientLocalRerankResult:
        """Validate a frontend-side result against current authenticated state."""

        if not isinstance(package, ClientLocalRerankPackage):
            raise TypeError("client_local_package_required")
        if not isinstance(payload, Mapping):
            raise TypeError("client_local_result_mapping_required")
        if not isinstance(current_prepared, PreparedBatch):
            raise TypeError("prepared_batch_required")
        if current_prepared.contract_version != PREPARED_INPUT_CONTRACT:
            raise ValueError("prepared_batch_contract_mismatch")

        project_id = _bounded_identifier(
            authenticated_project_id,
            reason="client_local_project_id_invalid",
        )
        request_id = _bounded_identifier(
            current_request_id,
            reason="client_local_request_id_invalid",
        )
        query = _bounded_query(current_query, max_query_bytes=self._max_query_bytes)
        candidate_set_version = _bounded_identifier(
            current_candidate_set_version,
            reason="client_local_candidate_set_version_invalid",
        )
        resolved_top_k = _resolve_top_k(
            current_prepared,
            current_top_k,
            reason="client_local_top_k_invalid",
        )
        if project_id != package.project_id:
            raise ValueError("client_local_project_mismatch")
        if request_id != package.request_id:
            raise ValueError("client_local_request_mismatch")
        if candidate_set_version != package.candidate_set_version:
            raise ValueError("client_local_candidate_set_version_mismatch")

        _validate_client_local_package(
            package,
            max_items=self._max_items,
            max_package_bytes=_DEFAULT_MAX_CLIENT_LOCAL_PACKAGE_BYTES,
            max_query_bytes=self._max_query_bytes,
            max_text_bytes=self._max_text_bytes,
        )
        if query != package.query or material_sha256(query) != package.query_hash:
            raise ValueError("client_local_query_mismatch")
        if resolved_top_k != package.top_k:
            raise ValueError("client_local_top_k_mismatch")
        expected_candidates = _client_local_candidates(current_prepared)
        expected_candidate_hash = _candidate_set_hash(current_prepared, candidate_set_version)
        if (
            expected_candidates != package.candidates
            or expected_candidate_hash != package.candidate_set_hash
            or current_prepared.embedding_identity != package.embedding_identity
            or current_prepared.embedding_dimension != package.embedding_dimension
        ):
            raise ValueError("client_local_candidate_set_mismatch")

        _require_mapping_fields(payload, _CLIENT_LOCAL_RESULT_FIELDS)
        if payload.get("contract_version") != CLIENT_LOCAL_RESULT_CONTRACT:
            raise ValueError("client_local_result_contract_mismatch")
        if payload.get("package_hash") != package.package_hash:
            raise ValueError("client_local_result_package_mismatch")
        reported_model_identity = _bounded_identifier(
            payload.get("model_identity"),
            reason="client_local_model_identity_invalid",
        )
        if reported_model_identity != package.model_identity:
            raise ValueError("client_local_model_identity_mismatch")
        ranked_items = _validated_client_local_items(
            payload.get("items"),
            allowed_ids={candidate.item_id for candidate in package.candidates},
            top_k=package.top_k,
        )
        return ClientLocalRerankResult(
            contract_version=CLIENT_LOCAL_RESULT_CONTRACT,
            scoring_version=CLIENT_LOCAL_SCORING_VERSION,
            project_id=package.project_id,
            request_id=package.request_id,
            package_hash=package.package_hash,
            candidate_set_version=package.candidate_set_version,
            candidate_set_hash=package.candidate_set_hash,
            query_hash=package.query_hash,
            reported_model_identity=reported_model_identity,
            top_k=package.top_k,
            items=ranked_items,
        )

    def accept_client_local_rerank_authoritative(
        self,
        *,
        package: ClientLocalRerankPackage,
        payload: Mapping[str, object],
        authenticated_project_id: str,
        current_request_id: str,
    ) -> ClientLocalRerankResult:
        """Validate a result against a package loaded from server storage.

        The normal ``accept_client_local_rerank`` method additionally checks a
        freshly prepared batch for an interactive request.  Durable jobs use
        this variant: the package and binding were persisted before the client
        received them, so accepting a client-supplied package would defeat the
        concurrency and integrity guarantees of the job store.
        """

        return accept_authoritative_client_local_rerank(
            package,
            payload,
            authenticated_project_id=authenticated_project_id,
            current_request_id=current_request_id,
            max_query_bytes=self._max_query_bytes,
            max_text_bytes=self._max_text_bytes,
        )

    def _parse_items(self, payloads: Sequence[Mapping[str, object]]) -> tuple[IntegratedInput, ...]:
        return _parse_integrated_inputs(
            payloads,
            max_items=self._max_items,
            max_text_bytes=self._max_text_bytes,
        )


def validate_rerank_submission(
    *,
    payloads: Sequence[Mapping[str, object]],
    query: object,
    project_id: object,
    request_id: object,
    idempotency_key: object,
    candidate_set_version: object,
    top_k: object = None,
    max_items: int = _DEFAULT_MAX_ITEMS,
    max_text_bytes: int = _DEFAULT_MAX_TEXT_BYTES,
    max_query_bytes: int = _DEFAULT_MAX_QUERY_BYTES,
) -> None:
    """Validate a submission without constructing an embedding provider."""

    resolved_max_items = _bounded_limit(
        max_items,
        default=_DEFAULT_MAX_ITEMS,
        maximum=_HARD_MAX_ITEMS,
        reason="input_max_items_invalid",
    )
    resolved_max_text_bytes = _bounded_limit(
        max_text_bytes,
        default=_DEFAULT_MAX_TEXT_BYTES,
        maximum=_HARD_MAX_TEXT_BYTES,
        reason="input_max_text_bytes_invalid",
    )
    resolved_max_query_bytes = _bounded_limit(
        max_query_bytes,
        default=_DEFAULT_MAX_QUERY_BYTES,
        maximum=_HARD_MAX_QUERY_BYTES,
        reason="rerank_max_query_bytes_invalid",
    )
    items = _parse_integrated_inputs(
        payloads,
        max_items=resolved_max_items,
        max_text_bytes=resolved_max_text_bytes,
    )
    _bounded_query(query, max_query_bytes=resolved_max_query_bytes)
    _bounded_identifier(project_id, reason="rerank_project_id_invalid")
    _bounded_identifier(request_id, reason="rerank_request_id_invalid")
    _bounded_identifier(idempotency_key, reason="rerank_idempotency_key_invalid")
    _bounded_identifier(
        candidate_set_version,
        reason="rerank_candidate_set_version_invalid",
    )
    if top_k is not None:
        _bounded_limit(
            top_k,
            default=len(items),
            maximum=len(items),
            reason="rerank_top_k_invalid",
        )


def _parse_integrated_inputs(
    payloads: Sequence[Mapping[str, object]],
    *,
    max_items: int,
    max_text_bytes: int,
) -> tuple[IntegratedInput, ...]:
    if isinstance(payloads, (str, bytes)) or not isinstance(payloads, Sequence):
        raise TypeError("input_items_sequence_required")
    if not payloads or len(payloads) > max_items:
        raise ValueError("input_item_count_invalid")
    items: list[IntegratedInput] = []
    seen_ids: set[str] = set()
    for payload in payloads:
        if not isinstance(payload, Mapping):
            raise TypeError("input_item_mapping_required")
        item = IntegratedInput.from_mapping(payload, max_text_bytes=max_text_bytes)
        if item.item_id in seen_ids:
            raise ValueError("input_id_duplicate")
        seen_ids.add(item.item_id)
        items.append(item)
    return tuple(items)


def material_sha256(text: str) -> str:
    """Hash exact UTF-8 inference material with an explicit algorithm prefix."""

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def accept_authoritative_client_local_rerank(
    package: ClientLocalRerankPackage,
    payload: Mapping[str, object],
    *,
    authenticated_project_id: str,
    current_request_id: str,
    max_items: int = _DEFAULT_MAX_ITEMS,
    max_package_bytes: int = _DEFAULT_MAX_CLIENT_LOCAL_PACKAGE_BYTES,
    max_query_bytes: int = _DEFAULT_MAX_QUERY_BYTES,
    max_text_bytes: int = _DEFAULT_MAX_TEXT_BYTES,
) -> ClientLocalRerankResult:
    """Validate a durable client result without constructing an embedder."""

    if not isinstance(package, ClientLocalRerankPackage):
        raise TypeError("client_local_package_required")
    if not isinstance(payload, Mapping):
        raise TypeError("client_local_result_mapping_required")
    project_id = _bounded_identifier(
        authenticated_project_id,
        reason="client_local_project_id_invalid",
    )
    request_id = _bounded_identifier(
        current_request_id,
        reason="client_local_request_id_invalid",
    )
    if package.project_id != project_id:
        raise ValueError("client_local_project_mismatch")
    if package.request_id != request_id:
        raise ValueError("client_local_request_mismatch")
    _validate_client_local_package(
        package,
        max_items=_bounded_limit(
            max_items,
            default=_DEFAULT_MAX_ITEMS,
            maximum=_HARD_MAX_ITEMS,
            reason="input_max_items_invalid",
        ),
        max_package_bytes=_bounded_limit(
            max_package_bytes,
            default=_DEFAULT_MAX_CLIENT_LOCAL_PACKAGE_BYTES,
            maximum=_HARD_MAX_CLIENT_LOCAL_PACKAGE_BYTES,
            reason="client_local_max_package_bytes_invalid",
        ),
        max_query_bytes=max_query_bytes,
        max_text_bytes=max_text_bytes,
    )
    _require_mapping_fields(payload, _CLIENT_LOCAL_RESULT_FIELDS)
    if payload.get("contract_version") != CLIENT_LOCAL_RESULT_CONTRACT:
        raise ValueError("client_local_result_contract_mismatch")
    if payload.get("package_hash") != package.package_hash:
        raise ValueError("client_local_result_package_mismatch")
    reported_model_identity = _bounded_identifier(
        payload.get("model_identity"),
        reason="client_local_model_identity_invalid",
    )
    if reported_model_identity != package.model_identity:
        raise ValueError("client_local_model_identity_mismatch")
    ranked_items = _validated_client_local_items(
        payload.get("items"),
        allowed_ids={candidate.item_id for candidate in package.candidates},
        top_k=package.top_k,
    )
    return ClientLocalRerankResult(
        contract_version=CLIENT_LOCAL_RESULT_CONTRACT,
        scoring_version=CLIENT_LOCAL_SCORING_VERSION,
        project_id=package.project_id,
        request_id=package.request_id,
        package_hash=package.package_hash,
        candidate_set_version=package.candidate_set_version,
        candidate_set_hash=package.candidate_set_hash,
        query_hash=package.query_hash,
        reported_model_identity=reported_model_identity,
        top_k=package.top_k,
        items=ranked_items,
    )


def validate_client_local_rerank_package(
    package: ClientLocalRerankPackage,
    *,
    max_items: int = _DEFAULT_MAX_ITEMS,
    max_package_bytes: int = _DEFAULT_MAX_CLIENT_LOCAL_PACKAGE_BYTES,
    max_query_bytes: int = _DEFAULT_MAX_QUERY_BYTES,
    max_text_bytes: int = _DEFAULT_MAX_TEXT_BYTES,
) -> None:
    """Validate an immutable server-issued package before local model work."""

    if not isinstance(package, ClientLocalRerankPackage):
        raise TypeError("client_local_package_required")
    _validate_client_local_package(
        package,
        max_items=_bounded_limit(
            max_items,
            default=_DEFAULT_MAX_ITEMS,
            maximum=_HARD_MAX_ITEMS,
            reason="input_max_items_invalid",
        ),
        max_package_bytes=_bounded_limit(
            max_package_bytes,
            default=_DEFAULT_MAX_CLIENT_LOCAL_PACKAGE_BYTES,
            maximum=_HARD_MAX_CLIENT_LOCAL_PACKAGE_BYTES,
            reason="client_local_max_package_bytes_invalid",
        ),
        max_query_bytes=_bounded_limit(
            max_query_bytes,
            default=_DEFAULT_MAX_QUERY_BYTES,
            maximum=_HARD_MAX_QUERY_BYTES,
            reason="rerank_max_query_bytes_invalid",
        ),
        max_text_bytes=_bounded_limit(
            max_text_bytes,
            default=_DEFAULT_MAX_TEXT_BYTES,
            maximum=_HARD_MAX_TEXT_BYTES,
            reason="input_max_text_bytes_invalid",
        ),
    )


def _require_mapping_fields(payload: Mapping[str, object], allowed: frozenset[str]) -> None:
    if not all(isinstance(key, str) for key in payload):
        raise ValueError("input_field_not_allowed")
    if set(payload) - allowed:
        raise ValueError("input_field_not_allowed")


def _bounded_identifier(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError(reason)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(reason) from None
    if size > _MAX_IDENTIFIER_BYTES:
        raise ValueError(reason)
    return value


def _bounded_text(value: object, *, max_text_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("input_text_invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("input_text_invalid") from None
    if size > max_text_bytes:
        raise ValueError("input_text_too_large")
    return value


def _bounded_query(value: object, *, max_query_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("rerank_query_invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("rerank_query_invalid") from None
    if size > max_query_bytes:
        raise ValueError("rerank_query_too_large")
    return value


def _bounded_score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("input_base_score_invalid")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError("input_base_score_invalid")
    return score


def _positive_int(value: object, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(reason)
    return value


def _bounded_limit(
    value: object,
    *,
    default: int,
    maximum: int,
    reason: str,
) -> int:
    resolved = default if value is None else value
    if isinstance(resolved, bool) or not isinstance(resolved, int):
        raise ValueError(reason)
    if not 0 < resolved <= maximum:
        raise ValueError(reason)
    return resolved


def _resolve_top_k(prepared: PreparedBatch, top_k: int | None, *, reason: str) -> int:
    if top_k is None:
        return len(prepared.items)
    return _bounded_limit(
        top_k,
        default=len(prepared.items),
        maximum=max(len(prepared.items), 1),
        reason=reason,
    )


def _validated_vector(value: object, dimension: int, *, prefix: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{prefix}_vector_invalid")
    if len(value) != dimension:
        raise ValueError(f"{prefix}_dimension_mismatch")
    vector: list[float] = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ValueError(f"{prefix}_value_invalid")
        numeric = float(component)
        if not math.isfinite(numeric):
            raise ValueError(f"{prefix}_value_invalid")
        vector.append(numeric)
    if not any(component != 0.0 for component in vector):
        raise ValueError(f"{prefix}_zero_vector")
    return tuple(vector)


def _candidate_set_hash(prepared: PreparedBatch, candidate_set_version: str) -> str:
    return _canonical_sha256(
        {
            "candidate_set_version": candidate_set_version,
            "embedding_identity": prepared.embedding_identity,
            "embedding_dimension": prepared.embedding_dimension,
            "items": [
                {
                    "id": item.item_id,
                    "material_sha256": item.material_sha256,
                    "base_score": item.base_score,
                    "embedding_sha256": _canonical_sha256({"vector": item.embedding}),
                }
                for item in prepared.items
            ],
        }
    )


def _client_local_candidates(prepared: PreparedBatch) -> tuple[ClientLocalCandidate, ...]:
    return tuple(
        ClientLocalCandidate(
            item_id=item.item_id,
            text=item.text,
            base_score=item.base_score,
            material_sha256=item.material_sha256,
            embedding_sha256=_canonical_sha256({"vector": item.embedding}),
        )
        for item in prepared.items
    )


def _client_local_package_material(
    *,
    project_id: str,
    request_id: str,
    candidate_set_version: str,
    candidate_set_hash: str,
    query: str,
    query_hash: str,
    embedding_identity: str,
    embedding_dimension: int,
    model_identity: str,
    top_k: int,
    candidates: tuple[ClientLocalCandidate, ...],
) -> Mapping[str, object]:
    return {
        "contract_version": CLIENT_LOCAL_RERANK_CONTRACT,
        "scoring_version": CLIENT_LOCAL_SCORING_VERSION,
        "project_id": project_id,
        "request_id": request_id,
        "candidate_set_version": candidate_set_version,
        "candidate_set_hash": candidate_set_hash,
        "query": query,
        "query_hash": query_hash,
        "embedding_identity": embedding_identity,
        "embedding_dimension": embedding_dimension,
        "model_identity": model_identity,
        "top_k": top_k,
        "candidates": [
            {
                "id": candidate.item_id,
                "text": candidate.text,
                "base_score": candidate.base_score,
                "material_sha256": candidate.material_sha256,
                "embedding_sha256": candidate.embedding_sha256,
            }
            for candidate in candidates
        ],
    }


def _validate_client_local_package(
    package: ClientLocalRerankPackage,
    *,
    max_items: int,
    max_package_bytes: int,
    max_query_bytes: int,
    max_text_bytes: int,
) -> None:
    if package.contract_version != CLIENT_LOCAL_RERANK_CONTRACT:
        raise ValueError("client_local_package_contract_mismatch")
    if package.scoring_version != CLIENT_LOCAL_SCORING_VERSION:
        raise ValueError("client_local_scoring_version_mismatch")
    _bounded_identifier(package.project_id, reason="client_local_project_id_invalid")
    _bounded_identifier(package.request_id, reason="client_local_request_id_invalid")
    _bounded_identifier(
        package.candidate_set_version,
        reason="client_local_candidate_set_version_invalid",
    )
    _bounded_identifier(
        package.embedding_identity,
        reason="client_local_embedding_identity_invalid",
    )
    _bounded_identifier(
        package.model_identity,
        reason="client_local_model_identity_invalid",
    )
    _bounded_query(package.query, max_query_bytes=max_query_bytes)
    _positive_int(package.embedding_dimension, "client_local_embedding_dimension_invalid")
    if package.query_hash != material_sha256(package.query):
        raise ValueError("client_local_query_hash_mismatch")
    if not _SHA256_RE.fullmatch(package.candidate_set_hash):
        raise ValueError("client_local_candidate_set_hash_invalid")
    if (
        not isinstance(package.candidates, tuple)
        or not package.candidates
        or len(package.candidates) > max_items
    ):
        raise ValueError("client_local_candidates_invalid")
    _bounded_limit(
        package.top_k,
        default=len(package.candidates),
        maximum=len(package.candidates),
        reason="client_local_top_k_invalid",
    )

    seen_ids: set[str] = set()
    for candidate in package.candidates:
        if not isinstance(candidate, ClientLocalCandidate):
            raise ValueError("client_local_candidates_invalid")
        item_id = _bounded_identifier(candidate.item_id, reason="client_local_candidate_id_invalid")
        if item_id in seen_ids:
            raise ValueError("client_local_candidate_id_duplicate")
        seen_ids.add(item_id)
        _bounded_text(candidate.text, max_text_bytes=max_text_bytes)
        _bounded_score(candidate.base_score)
        if candidate.material_sha256 != material_sha256(candidate.text):
            raise ValueError("client_local_candidate_material_hash_mismatch")
        if not _SHA256_RE.fullmatch(candidate.embedding_sha256):
            raise ValueError("client_local_candidate_embedding_hash_invalid")

    package_material = _client_local_package_material(
        project_id=package.project_id,
        request_id=package.request_id,
        candidate_set_version=package.candidate_set_version,
        candidate_set_hash=package.candidate_set_hash,
        query=package.query,
        query_hash=package.query_hash,
        embedding_identity=package.embedding_identity,
        embedding_dimension=package.embedding_dimension,
        model_identity=package.model_identity,
        top_k=package.top_k,
        candidates=package.candidates,
    )
    if len(_canonical_json(package_material).encode("utf-8")) > max_package_bytes:
        raise ValueError("client_local_package_too_large")
    expected_package_hash = _canonical_sha256(package_material)
    if package.package_hash != expected_package_hash:
        raise ValueError("client_local_package_hash_mismatch")


def _validated_client_local_items(
    value: object,
    *,
    allowed_ids: set[str],
    top_k: int,
) -> tuple[RankedInput, ...]:
    if not isinstance(value, list) or len(value) != top_k:
        raise ValueError("client_local_result_items_invalid")
    seen_ids: set[str] = set()
    output: list[RankedInput] = []
    previous_score = math.inf
    for rank, row in enumerate(value, start=1):
        if not isinstance(row, Mapping):
            raise ValueError("client_local_result_items_invalid")
        _require_mapping_fields(row, _CLIENT_LOCAL_RESULT_ITEM_FIELDS)
        item_id = _bounded_identifier(row.get("id"), reason="client_local_result_id_invalid")
        if item_id not in allowed_ids or item_id in seen_ids:
            raise ValueError("client_local_result_id_invalid")
        try:
            score = _bounded_score(row.get("score"))
        except ValueError:
            raise ValueError("client_local_result_score_invalid") from None
        if score > previous_score:
            raise ValueError("client_local_result_order_invalid")
        previous_score = score
        seen_ids.add(item_id)
        output.append(RankedInput(item_id=item_id, score=score, rank=rank))
    return tuple(output)


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = _canonical_json(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _runtime_rerank_policy_revision() -> str:
    """Hash non-secret server policy so model/endpoint drift changes requests."""

    names = (
        "PP_RERANK_PROVIDERS",
        "PP_RERANK_BASE_URL",
        "PP_RERANK_PATH",
        "PP_RERANK_CLOUD_MODEL",
        "PP_RERANK_CLOUD_MODEL_REVISION",
        "PP_RERANK_OLLAMA_MODEL",
        "PP_RERANK_MODEL",
        "PP_RERANK_MODEL_REVISION",
        "PP_RERANK_MAX_CANDIDATES",
        "PP_RERANK_MAX_DOCUMENT_CHARS",
        "PP_RERANK_MAX_QUERY_CHARS",
    )
    return _canonical_sha256({name: os.getenv(name, "") for name in names})


def _validated_ranked_items(
    ranked: object,
    prepared: PreparedBatch,
    top_k: int,
) -> tuple[RankedInput, ...]:
    if not isinstance(ranked, list) or len(ranked) > top_k:
        raise RuntimeError("rerank_response_invalid")
    expected_ids = {item.item_id for item in prepared.items}
    seen_ids: set[str] = set()
    output: list[RankedInput] = []
    for rank, row in enumerate(ranked, start=1):
        if not isinstance(row, tuple) or len(row) != 2:
            raise RuntimeError("rerank_response_invalid")
        item_id, raw_score = row
        if not isinstance(item_id, str) or item_id not in expected_ids or item_id in seen_ids:
            raise RuntimeError("rerank_response_invalid")
        score = _bounded_score(raw_score)
        seen_ids.add(item_id)
        output.append(RankedInput(item_id=item_id, score=score, rank=rank))
    if len(output) != min(top_k, len(prepared.items)):
        raise RuntimeError("rerank_response_incomplete")
    return tuple(output)


def _validated_authoritative_ranked_items(
    ranked: object,
    *,
    allowed_ids: set[str],
    top_k: int,
) -> tuple[RankedInput, ...]:
    """Validate provider tuples when only a durable package is available."""

    if not isinstance(ranked, list) or len(ranked) > top_k:
        raise RuntimeError("rerank_response_invalid")
    seen_ids: set[str] = set()
    output: list[RankedInput] = []
    for rank, row in enumerate(ranked, start=1):
        if not isinstance(row, tuple) or len(row) != 2:
            raise RuntimeError("rerank_response_invalid")
        item_id, raw_score = row
        if not isinstance(item_id, str) or item_id not in allowed_ids or item_id in seen_ids:
            raise RuntimeError("rerank_response_invalid")
        score = _bounded_score(raw_score)
        seen_ids.add(item_id)
        output.append(RankedInput(item_id=item_id, score=score, rank=rank))
    if len(output) != min(top_k, len(allowed_ids)):
        raise RuntimeError("rerank_response_incomplete")
    return tuple(output)


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        frozen_item = _freeze_value(item)
        if frozen_item is not _UNSUPPORTED_DIAGNOSTIC_VALUE:
            frozen[key] = frozen_item
    return MappingProxyType(frozen)


_UNSUPPORTED_DIAGNOSTIC_VALUE = object()


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        frozen_items = []
        for item in value:
            frozen_item = _freeze_value(item)
            if frozen_item is not _UNSUPPORTED_DIAGNOSTIC_VALUE:
                frozen_items.append(frozen_item)
        return tuple(frozen_items)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return _UNSUPPORTED_DIAGNOSTIC_VALUE


__all__ = [
    "BackendInferenceService",
    "CLIENT_LOCAL_RERANK_CONTRACT",
    "CLIENT_LOCAL_RESULT_CONTRACT",
    "CLIENT_LOCAL_SCORING_VERSION",
    "ClientLocalCandidate",
    "ClientLocalRerankPackage",
    "ClientLocalRerankResult",
    "IntegratedInput",
    "PREPARED_INPUT_CONTRACT",
    "PreparedBatch",
    "PreparedInput",
    "ProvidedEmbedding",
    "RERANK_CONTRACT",
    "RERANK_REQUEST_CONTRACT",
    "RERANK_SCORING_VERSION",
    "RankedInput",
    "RerankRequestBinding",
    "RerankResult",
    "accept_authoritative_client_local_rerank",
    "material_sha256",
    "validate_client_local_rerank_package",
    "validate_rerank_submission",
]
