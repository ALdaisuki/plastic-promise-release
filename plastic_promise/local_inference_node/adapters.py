"""Optional local-model adapters behind the node's inference-only seam.

Imports of heavyweight model libraries are intentionally lazy.  Starting the
node never downloads a model: callers must make a fixed model revision available
in a local read-only cache or mounted directory first.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from plastic_promise.core.embedder import OllamaEmbedder
from plastic_promise.core.provider_http import (
    ProviderHTTPClient,
    ProviderHTTPError,
    ProviderHTTPPolicy,
)
from plastic_promise.core.structured_token_budget import (
    UNBOUNDED_STRUCTURED_TOKEN_LIMIT,
    structured_tokens_allowed,
    validate_structured_token_limit,
)

from .contract import EmbeddingEngine, EmbeddingProviderIdentity, NodeConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class NodeModelUnavailableError(RuntimeError):
    """Raised when an explicitly configured local model is not available."""


class NodeModelIdentityDriftError(NodeModelUnavailableError):
    """Raised when a model result cannot be bound to the declared artifact."""


class IdentityBoundEmbeddingFallback:
    """Use a fallback embedding adapter only when its identity is exact."""

    def __init__(
        self,
        *,
        primary: EmbeddingEngine,
        primary_identity: EmbeddingProviderIdentity,
        fallback: EmbeddingEngine,
        fallback_identity: EmbeddingProviderIdentity,
    ) -> None:
        if primary_identity != fallback_identity:
            raise NodeConfigurationError("node_embedding_fallback_identity_mismatch")
        self._primary = primary
        self._fallback = fallback

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._primary.embed_batch(texts)
        except NodeModelIdentityDriftError:
            # Identity drift is a governance failure, not an availability
            # outage; silently switching providers would hide quarantine data.
            raise
        except NodeModelUnavailableError:
            return self._fallback.embed_batch(texts)


class CloudEmbeddingAdapter:
    """OpenAI-compatible cloud embeddings behind the compute-node seam."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        path: str,
        model: str,
        revision: str | None = None,
        expected_dimension: int | None = None,
        dimension: int | None = None,
        normalization: str = "l2",
        send_dimensions: bool = True,
        policy: ProviderHTTPPolicy | None = None,
        client: object | None = None,
    ) -> None:
        if normalization not in {"l2", "none"}:
            raise ValueError("node_embedding_normalization_unsupported")
        if (
            expected_dimension is not None
            and dimension is not None
            and expected_dimension != dimension
        ):
            raise ValueError("node_embedding_dimension_conflict")
        resolved_dimension = expected_dimension if expected_dimension is not None else dimension
        if (
            resolved_dimension is None
            or not isinstance(resolved_dimension, int)
            or isinstance(resolved_dimension, bool)
            or resolved_dimension <= 0
        ):
            raise ValueError("node_embedding_dimension_invalid")
        self._model = _required_provider_text(model, "node_cloud_embedding_model_invalid")
        self._revision = _required_provider_text(revision, "node_cloud_embedding_revision_invalid")
        self._path = _required_provider_path(path, "node_cloud_embedding_path_invalid")
        self._expected_dimension = resolved_dimension
        self._normalization = normalization
        self._send_dimensions = bool(send_dimensions)
        if client is not None:
            self._client = client
        else:
            if not isinstance(api_key, str) or not isinstance(base_url, str):
                raise ValueError("node_cloud_embedding_credentials_missing")
            self._client = ProviderHTTPClient(
                provider="node-cloud-embedding",
                base_url=base_url,
                api_key=api_key,
                policy=policy,
            )

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {"model": self._model, "input": texts}
        if self._send_dimensions:
            payload["dimensions"] = self._expected_dimension
        try:
            response = self._client.post_json(self._path, payload)
            response_payload = _provider_payload(response)
            _validate_cloud_identity(
                response_payload,
                expected_model=self._model,
                expected_revision=self._revision,
                reason="node_cloud_embedding_identity_mismatch",
            )
            vectors = _cloud_embedding_vectors(
                response_payload,
                expected_model=self._model,
                expected_count=len(texts),
                expected_dimension=self._expected_dimension,
            )
            return _apply_normalization(vectors, self._normalization)
        except ProviderHTTPError as exc:
            raise NodeModelUnavailableError("node_cloud_embedding_unavailable") from exc
        except (OverflowError, TypeError, ValueError) as exc:
            raise NodeModelUnavailableError("node_cloud_embedding_response_invalid") from exc


class CloudRerankingAdapter:
    """OpenAI-style cloud reranking behind the compute-node seam."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        path: str,
        model: str,
        revision: str | None = None,
        policy: ProviderHTTPPolicy | None = None,
        client: object | None = None,
    ) -> None:
        self._model = _required_provider_text(model, "node_cloud_rerank_model_invalid")
        self._revision = _required_provider_text(revision, "node_cloud_rerank_revision_invalid")
        self._path = _required_provider_path(path, "node_cloud_rerank_path_invalid")
        if client is not None:
            self._client = client
        else:
            if not isinstance(api_key, str) or not isinstance(base_url, str):
                raise ValueError("node_cloud_rerank_credentials_missing")
            self._client = ProviderHTTPClient(
                provider="node-cloud-rerank",
                base_url=base_url,
                api_key=api_key,
                policy=policy,
            )

    def rerank_tuples(
        self,
        query: str,
        candidates: Sequence[tuple[int, str]],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        del top_k
        payload = {
            "model": self._model,
            "query": query,
            "documents": [text for _index, text in candidates],
            "top_n": len(candidates),
        }
        try:
            response = self._client.post_json(self._path, payload)
            response_payload = _provider_payload(response)
            _validate_cloud_identity(
                response_payload,
                expected_model=self._model,
                expected_revision=self._revision,
                reason="node_cloud_rerank_identity_mismatch",
            )
            scores = _cloud_rerank_scores(
                response_payload,
                candidate_count=len(candidates),
                expected_model=self._model,
            )
            return [(index, scores[position]) for position, (index, _text) in enumerate(candidates)]
        except ProviderHTTPError as exc:
            raise NodeModelUnavailableError("node_cloud_rerank_unavailable") from exc
        except (OverflowError, TypeError, ValueError) as exc:
            raise NodeModelUnavailableError("node_cloud_rerank_response_invalid") from exc


class CloudStructuredJSONAdapter:
    """OpenAI-compatible structured JSON inference owned by the compute node."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        path: str = "/chat/completions",
        model: str,
        revision: str | None = None,
        max_system_prompt_bytes: int = 32 * 1024,
        max_user_payload_bytes: int = 256 * 1024,
        max_output_bytes: int = 256 * 1024,
        max_tokens: int = UNBOUNDED_STRUCTURED_TOKEN_LIMIT,
        policy: ProviderHTTPPolicy | None = None,
        client: object | None = None,
    ) -> None:
        self._model = _required_provider_text(model, "node_cloud_structured_model_invalid")
        self._revision = _required_provider_text(
            revision,
            "node_cloud_structured_revision_invalid",
        )
        self._path = _required_provider_path(path, "node_cloud_structured_path_invalid")
        self._max_system_prompt_bytes = _positive_limit(
            max_system_prompt_bytes,
            "node_cloud_structured_system_limit_invalid",
        )
        self._max_user_payload_bytes = _positive_limit(
            max_user_payload_bytes,
            "node_cloud_structured_payload_limit_invalid",
        )
        self._max_output_bytes = _positive_limit(
            max_output_bytes,
            "node_cloud_structured_output_limit_invalid",
        )
        try:
            self._max_tokens = validate_structured_token_limit(max_tokens)
        except ValueError as exc:
            raise ValueError("node_cloud_structured_token_limit_invalid") from exc
        if client is not None:
            self._client = client
        else:
            if not isinstance(api_key, str) or not isinstance(base_url, str):
                raise ValueError("node_cloud_structured_credentials_missing")
            self._client = ProviderHTTPClient(
                provider="node-cloud-structured-json",
                base_url=base_url,
                api_key=api_key,
                policy=policy,
            )

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, object],
        max_tokens: int,
    ) -> dict[str, object]:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("node_structured_system_prompt_invalid")
        if len(system_prompt.encode("utf-8")) > self._max_system_prompt_bytes:
            raise ValueError("node_structured_system_prompt_too_large")
        serialized_payload = _bounded_json_object(
            user_payload,
            maximum=self._max_user_payload_bytes,
            reason="node_structured_user_payload_too_large",
        )
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or not structured_tokens_allowed(max_tokens, self._max_tokens)
        ):
            raise ValueError("node_structured_max_tokens_invalid")
        request_payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": serialized_payload},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._client.post_json(self._path, request_payload)
            response_payload = _provider_payload(response)
            _validate_cloud_identity(
                response_payload,
                expected_model=self._model,
                expected_revision=self._revision,
                reason="node_cloud_structured_identity_mismatch",
                allow_missing_revision=True,
            )
            content = _structured_response_content(response_payload)
            if len(content.encode("utf-8")) > self._max_output_bytes:
                raise ValueError("node_structured_output_too_large")
            decoded = json.loads(content, parse_constant=_reject_json_constant)
            if not isinstance(decoded, dict):
                raise ValueError("node_structured_output_object_required")
            _bounded_json_object(
                decoded,
                maximum=self._max_output_bytes,
                reason="node_structured_output_too_large",
            )
            return decoded
        except ProviderHTTPError as exc:
            raise NodeModelUnavailableError("node_cloud_structured_unavailable") from exc
        except (OverflowError, RecursionError, TypeError, ValueError) as exc:
            raise NodeModelUnavailableError("node_cloud_structured_response_invalid") from exc


class LlamaCppEmbeddingAdapter:
    """Call a local llama-server OpenAI-compatible embeddings endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        path: str = "/v1/embeddings",
        model: str,
        expected_dimension: int,
        normalization: str = "l2",
        client: object | None = None,
    ) -> None:
        if normalization not in {"l2", "none"}:
            raise ValueError("node_embedding_normalization_unsupported")
        if (
            not isinstance(expected_dimension, int)
            or isinstance(expected_dimension, bool)
            or expected_dimension <= 0
        ):
            raise ValueError("node_embedding_dimension_invalid")
        self._model = _required_provider_text(model, "node_llama_cpp_embedding_model_invalid")
        self._expected_dimension = expected_dimension
        self._normalization = normalization
        self._path = _required_provider_path(path, "node_llama_cpp_embedding_path_invalid")
        if client is not None:
            self._client = client
        else:
            self._client = ProviderHTTPClient(
                provider="node-llama-cpp-embedding",
                base_url=base_url,
                api_key=None,
                allow_unauthenticated_loopback=True,
            )

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            response = _provider_payload(
                self._client.post_json(self._path, {"model": self._model, "input": texts})
            )
            if response.get("model") not in {None, self._model}:
                raise ValueError("node_llama_cpp_embedding_model_mismatch")
            rows = response.get("data")
            if not isinstance(rows, list) or len(rows) != len(texts):
                raise ValueError("node_llama_cpp_embedding_count_mismatch")
            ordered: list[list[float] | None] = [None] * len(texts)
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError("node_llama_cpp_embedding_response_invalid")
                index = row.get("index")
                vector = row.get("embedding")
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or not 0 <= index < len(texts)
                    or ordered[index] is not None
                    or not isinstance(vector, list)
                    or len(vector) != self._expected_dimension
                ):
                    raise ValueError("node_llama_cpp_embedding_response_invalid")
                ordered[index] = [
                    _finite_float(value, "node_llama_cpp_embedding_response_invalid")
                    for value in vector
                ]
            if any(vector is None for vector in ordered):
                raise ValueError("node_llama_cpp_embedding_response_invalid")
            return _apply_normalization(
                [vector for vector in ordered if vector is not None],
                self._normalization,
            )
        except ProviderHTTPError as exc:
            raise NodeModelUnavailableError("node_llama_cpp_embedding_unavailable") from exc
        except (OverflowError, TypeError, ValueError) as exc:
            raise NodeModelUnavailableError("node_llama_cpp_embedding_response_invalid") from exc


class LlamaCppRerankingAdapter:
    """Call a llama-server-compatible structured rerank endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        path: str = "/rerank",
        model: str,
        client: object | None = None,
    ) -> None:
        self._model = _required_provider_text(model, "node_llama_cpp_rerank_model_invalid")
        self._path = _required_provider_path(path, "node_llama_cpp_rerank_path_invalid")
        if client is not None:
            self._client = client
        else:
            self._client = ProviderHTTPClient(
                provider="node-llama-cpp-rerank",
                base_url=base_url,
                api_key=None,
                allow_unauthenticated_loopback=True,
            )

    def rerank_tuples(
        self,
        query: str,
        candidates: Sequence[tuple[int, str]],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        del top_k
        try:
            response = _provider_payload(
                self._client.post_json(
                    self._path,
                    {
                        "model": self._model,
                        "query": query,
                        "documents": [text for _index, text in candidates],
                    },
                )
            )
            if response.get("model") not in {None, self._model}:
                raise ValueError("node_llama_cpp_rerank_model_mismatch")
            rows = response.get("results")
            if not isinstance(rows, list) or len(rows) != len(candidates):
                raise ValueError("node_llama_cpp_rerank_result_incomplete")
            ordered: list[float | None] = [None] * len(candidates)
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError("node_llama_cpp_rerank_response_invalid")
                index = row.get("index")
                score = row.get("score", row.get("relevance_score"))
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or not 0 <= index < len(candidates)
                    or ordered[index] is not None
                ):
                    raise ValueError("node_llama_cpp_rerank_result_incomplete")
                ordered[index] = _finite_float(score, "node_llama_cpp_rerank_response_invalid")
            if any(score is None for score in ordered):
                raise ValueError("node_llama_cpp_rerank_result_incomplete")
            return [
                (index, score)
                for index, score in zip(
                    (candidate_index for candidate_index, _text in candidates),
                    ordered,
                    strict=True,
                )
                if score is not None
            ]
        except ProviderHTTPError as exc:
            raise NodeModelUnavailableError("node_llama_cpp_rerank_unavailable") from exc
        except (OverflowError, TypeError, ValueError) as exc:
            raise NodeModelUnavailableError("node_llama_cpp_rerank_response_invalid") from exc


class OllamaEmbeddingAdapter:
    """Expose local Ollama embeddings through the fixed node protocol."""

    def __init__(
        self,
        *,
        host: str,
        model: str,
        expected_dimension: int,
        expected_artifact_sha256: str,
        identity_probe: Callable[[], str],
        normalization: str = "l2",
    ) -> None:
        if normalization not in {"l2", "none"}:
            raise ValueError("node_embedding_normalization_unsupported")
        if (
            not expected_artifact_sha256.startswith("sha256:")
            or len(expected_artifact_sha256) != 71
        ):
            raise ValueError("node_ollama_model_identity_invalid")
        if not callable(identity_probe):
            raise ValueError("node_ollama_model_identity_probe_invalid")
        self._delegate = OllamaEmbedder(
            host=host,
            model=model,
            expected_dim=expected_dimension,
        )
        self._expected_artifact_sha256 = expected_artifact_sha256
        self._identity_probe = identity_probe
        self._normalization = normalization

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._verify_artifact_identity()
        vectors = self._delegate.embed_batch(texts)
        self._verify_artifact_identity()
        return _apply_normalization(vectors, self._normalization)

    def _verify_artifact_identity(self) -> None:
        try:
            observed = self._identity_probe()
        except Exception as exc:
            raise NodeModelUnavailableError("node_ollama_model_identity_unavailable") from exc
        if observed != self._expected_artifact_sha256:
            raise NodeModelIdentityDriftError("node_ollama_model_identity_drift")


class SentenceTransformersEmbeddingAdapter:
    """Run BGE embeddings from a pre-existing local model cache."""

    def __init__(
        self,
        *,
        model_reference: str,
        revision: str,
        cache_dir: Path | None = None,
        normalization: str = "l2",
        loader: Callable[..., Any] | None = None,
    ) -> None:
        if normalization not in {"l2", "none"}:
            raise ValueError("node_embedding_normalization_unsupported")
        try:
            model_loader = loader or _sentence_transformer_loader()
            self._model = model_loader(
                model_reference,
                revision=revision,
                cache_folder=str(cache_dir) if cache_dir is not None else None,
                local_files_only=True,
            )
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            raise NodeModelUnavailableError("node_embedding_model_unavailable") from exc
        self._normalization = normalization

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            vectors = self._model.encode(
                texts,
                batch_size=min(len(texts), 32),
                normalize_embeddings=self._normalization == "l2",
                convert_to_numpy=False,
                show_progress_bar=False,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise NodeModelUnavailableError("node_embedding_inference_failed") from exc
        return [_as_float_list(vector) for vector in vectors]


class LocalBgeReranker:
    """Run BGE reranking locally with no network model download fallback."""

    def __init__(
        self,
        *,
        model_reference: str,
        revision: str,
        cache_dir: Path | None = None,
        device: str | None = None,
        max_length: int = 512,
        loader: Callable[..., tuple[Any, Any, Any]] | None = None,
    ) -> None:
        if max_length <= 0:
            raise ValueError("node_rerank_max_length_invalid")
        try:
            model_loader = loader or _bge_reranker_loader()
            tokenizer, model, torch = model_loader(
                model_reference,
                revision=revision,
                cache_dir=cache_dir,
            )
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            raise NodeModelUnavailableError("node_rerank_model_unavailable") from exc

        selected_device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._tokenizer = tokenizer
        self._model = model.to(selected_device)
        self._model.eval()
        self._torch = torch
        self._device = selected_device
        self._max_length = max_length

    def rerank_tuples(
        self,
        query: str,
        candidates: Sequence[tuple[int, str]],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        """Return a score for every candidate; server policy applies the cut."""

        del top_k
        try:
            encoded = self._tokenizer(
                [query] * len(candidates),
                [text for _index, text in candidates],
                padding=True,
                truncation=True,
                max_length=self._max_length,
                return_tensors="pt",
            )
            inputs = {
                name: value.to(self._device) if hasattr(value, "to") else value
                for name, value in encoded.items()
            }
            with self._torch.inference_mode():
                logits = self._model(**inputs).logits
                scores = self._torch.sigmoid(logits.view(-1)).detach().cpu().float().tolist()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise NodeModelUnavailableError("node_rerank_inference_failed") from exc
        return [
            (index, float(score)) for (index, _text), score in zip(candidates, scores, strict=True)
        ]


class Qwen3CrossEncoderReranker:
    """Run Qwen3 rerankers through the official CrossEncoder interface.

    Sentence-Transformers exposes Qwen3-Reranker as a CrossEncoder; its
    default scores are raw logit differences and the official README offers
    an optional Sigmoid for 0-1 probabilities.  We keep the raw logit scores
    (monotonic ordering compatible with the fixed rerank/v1 contract), load
    only from the immutable local model tree, and never fall back to a
    network download.
    """

    def __init__(
        self,
        *,
        model_reference: str,
        revision: str,
        cache_dir: Path | None = None,
        device: str | None = None,
        # Qwen3-Reranker upstream context is 32K; keep the governed limit at
        # the official maximum rather than an undocumented lower bound.
        max_length: int = 32768,
        loader: Callable[..., Any] | None = None,
    ) -> None:
        if max_length <= 0:
            raise ValueError("node_rerank_max_length_invalid")
        try:
            model_loader = loader or _qwen3_cross_encoder_loader()
            self._model = model_loader(
                model_reference,
                revision=revision,
                cache_dir=cache_dir,
                device=device,
                max_length=max_length,
            )
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            raise NodeModelUnavailableError("node_rerank_model_unavailable") from exc
        self._max_length = max_length

    def rerank_tuples(
        self,
        query: str,
        candidates: Sequence[tuple[int, str]],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        """Return a score for every candidate; server policy applies the cut."""

        del top_k
        pairs = [(query, text) for _index, text in candidates]
        try:
            scores = self._model.predict(pairs)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise NodeModelUnavailableError("node_rerank_inference_failed") from exc
        if len(scores) != len(candidates):
            raise NodeModelUnavailableError("node_rerank_result_incomplete")
        return [
            (index, float(score)) for (index, _text), score in zip(candidates, scores, strict=True)
        ]


def _sentence_transformer_loader() -> Callable[..., Any]:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - depends on optional extra.
        raise NodeModelUnavailableError("node_local_inference_extra_required") from exc
    return SentenceTransformer


def _bge_reranker_loader() -> Callable[..., tuple[Any, Any, Any]]:
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as exc:  # pragma: no cover - depends on optional extra.
        raise NodeModelUnavailableError("node_local_inference_extra_required") from exc

    def load(
        model_reference: str, *, revision: str, cache_dir: Path | None
    ) -> tuple[Any, Any, Any]:
        cache_directory = str(cache_dir) if cache_dir is not None else None
        tokenizer = AutoTokenizer.from_pretrained(
            model_reference,
            revision=revision,
            cache_dir=cache_directory,
            local_files_only=True,
        )
        model = AutoModelForSequenceClassification.from_pretrained(
            model_reference,
            revision=revision,
            cache_dir=cache_directory,
            local_files_only=True,
        )
        return tokenizer, model, torch

    return load


def _qwen3_cross_encoder_loader() -> Callable[..., Any]:
    try:
        import torch
        from sentence_transformers import CrossEncoder
    except ImportError as exc:  # pragma: no cover - depends on optional extra.
        raise NodeModelUnavailableError("node_local_inference_extra_required") from exc

    def load(
        model_reference: str,
        *,
        revision: str,
        cache_dir: Path | None,
        device: str | None,
        max_length: int,
    ) -> Any:
        cache_directory = str(cache_dir) if cache_dir is not None else None
        selected_device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        return CrossEncoder(
            model_reference,
            revision=revision,
            cache_folder=cache_directory,
            local_files_only=True,
            device=selected_device,
            max_length=max_length,
        )

    return load


def _as_float_list(vector: Any) -> list[float]:
    raw_values = vector.tolist() if hasattr(vector, "tolist") else vector
    if not isinstance(raw_values, (list, tuple)):
        raise NodeModelUnavailableError("node_embedding_response_invalid")
    return [float(value) for value in raw_values]


def _provider_payload(response: object) -> Mapping[str, object]:
    payload = getattr(response, "payload", response)
    if not isinstance(payload, Mapping):
        raise ValueError("node_cloud_response_invalid")
    return payload


def _validate_cloud_identity(
    payload: Mapping[str, object],
    *,
    expected_model: str,
    expected_revision: str,
    reason: str,
    allow_missing_revision: bool = False,
) -> None:
    """Validate provider identity without inventing evidence it did not return.

    Embedding and rerank providers remain strict because their response
    identity is part of the derived-index contract.  OpenAI-compatible chat
    APIs commonly echo the exact model but omit a revision field; for
    structured JSON, the configured pinned revision therefore represents the
    node's deployment identity.  If the provider does echo a revision it must
    still match exactly.
    """

    if payload.get("model") != expected_model:
        raise ValueError(reason)
    observed_revision = payload.get("model_revision", payload.get("revision"))
    if observed_revision is None and allow_missing_revision:
        return
    if observed_revision != expected_revision:
        raise ValueError(reason)


def _cloud_embedding_vectors(
    payload: Mapping[str, object],
    *,
    expected_model: str,
    expected_count: int,
    expected_dimension: int,
) -> list[list[float]]:
    response_model = payload.get("model")
    if response_model != expected_model:
        raise ValueError("node_cloud_embedding_model_mismatch")
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise ValueError("node_cloud_embedding_count_mismatch")
    ordered: list[list[float] | None] = [None] * expected_count
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("node_cloud_embedding_response_invalid")
        index = row.get("index")
        vector = row.get("embedding")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < expected_count
            or ordered[index] is not None
            or not isinstance(vector, list)
            or len(vector) != expected_dimension
        ):
            raise ValueError("node_cloud_embedding_response_invalid")
        converted = [
            _finite_float(value, "node_cloud_embedding_response_invalid") for value in vector
        ]
        if not any(value != 0.0 for value in converted):
            raise ValueError("node_cloud_embedding_zero_vector")
        ordered[index] = converted
    if any(vector is None for vector in ordered):
        raise ValueError("node_cloud_embedding_response_invalid")
    return [vector for vector in ordered if vector is not None]


def _cloud_rerank_scores(
    payload: Mapping[str, object],
    *,
    candidate_count: int,
    expected_model: str,
) -> list[float]:
    if payload.get("model") != expected_model:
        raise ValueError("node_cloud_rerank_model_mismatch")
    rows = payload.get("results")
    if not isinstance(rows, list) or len(rows) != candidate_count:
        raise ValueError("node_cloud_rerank_result_incomplete")
    ordered: list[float | None] = [None] * candidate_count
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("node_cloud_rerank_response_invalid")
        index = row.get("index")
        score = row.get("score", row.get("relevance_score"))
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < candidate_count
            or ordered[index] is not None
        ):
            raise ValueError("node_cloud_rerank_result_incomplete")
        ordered[index] = _finite_float(score, "node_cloud_rerank_response_invalid")
    if any(score is None for score in ordered):
        raise ValueError("node_cloud_rerank_result_incomplete")
    return [score for score in ordered if score is not None]


def _structured_response_content(payload: Mapping[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("node_cloud_structured_response_invalid")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise ValueError("node_cloud_structured_response_invalid")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("node_cloud_structured_response_invalid")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("node_cloud_structured_response_invalid")
    return content


def _bounded_json_object(value: object, *, maximum: int, reason: str) -> str:
    if not isinstance(value, dict):
        raise ValueError("node_structured_payload_object_required")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise ValueError("node_structured_payload_invalid") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError(reason)
    return encoded


def _finite_float(value: object, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(reason)
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(reason)
    return converted


def _required_provider_text(value: str, reason: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(reason)
    return value.strip()


def _required_provider_path(value: str, reason: str) -> str:
    path = _required_provider_text(value, reason)
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError(reason)
    return path


def _positive_limit(value: int, reason: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(reason)
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("node_structured_output_nonfinite")


def _apply_normalization(vectors: list[list[float]], normalization: str) -> list[list[float]]:
    if normalization == "none":
        return vectors
    normalized: list[list[float]] = []
    for vector in vectors:
        magnitude = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(magnitude) or magnitude == 0:
            raise NodeModelUnavailableError("node_embedding_normalization_failed")
        normalized.append([value / magnitude for value in vector])
    return normalized
