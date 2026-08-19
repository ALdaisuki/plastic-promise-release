"""Non-secret runtime configuration for a loopback-only local inference node.

The Ollama peer may also be reached through the explicit Docker Desktop/WSL2
`host.docker.internal` gateway; public hosts are always rejected.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from urllib.request import urlopen

from plastic_promise.core.structured_token_budget import UNBOUNDED_STRUCTURED_TOKEN_LIMIT

from .adapters import (
    CloudEmbeddingAdapter,
    CloudRerankingAdapter,
    CloudStructuredJSONAdapter,
    LlamaCppEmbeddingAdapter,
    LlamaCppRerankingAdapter,
    LocalBgeReranker,
    OllamaEmbeddingAdapter,
    Qwen3CrossEncoderReranker,
    SentenceTransformersEmbeddingAdapter,
)
from .contract import (
    EmbeddingEngine,
    NodeConfigurationError,
    NodeIdentity,
    NodeLimits,
    RerankingEngine,
    StructuredJSONEngine,
)
from .resource_guard import NodeResourceGuard, resource_guard_from_environment

if TYPE_CHECKING:
    from collections.abc import Mapping

_EMBEDDING_BACKENDS = frozenset({"bge-local", "llama.cpp", "ollama", "cloud", "openai-compatible"})
_RERANK_BACKENDS = frozenset({"bge-local", "llama.cpp", "qwen3-cross-encoder", "cloud", "openai-compatible"})
_STRUCTURED_JSON_BACKENDS = frozenset({"off", "cloud", "openai-compatible"})
_EMBEDDING_NORMALIZATIONS = frozenset({"l2", "none"})


@dataclass(frozen=True)
class NodeCloudProviderConfig:
    """Node-private provider settings that never enter public identity JSON."""

    api_key: str = field(repr=False)
    base_url: str = field(repr=False)
    path: str
    send_dimensions: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise NodeConfigurationError("node_cloud_api_key_missing")
        if not isinstance(self.base_url, str):
            raise NodeConfigurationError("node_cloud_base_url_invalid")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise NodeConfigurationError("node_cloud_base_url_invalid")
        if (
            not isinstance(self.path, str)
            or not self.path.startswith("/")
            or self.path.startswith("//")
        ):
            raise NodeConfigurationError("node_cloud_path_invalid")


@dataclass(frozen=True)
class NodeRuntimeConfig:
    """Configuration that is safe to derive from a node-local environment."""

    identity: NodeIdentity
    bind_host: str
    port: int
    max_concurrency: int
    embedding_backend: str
    embedding_model_reference: Path | None
    embedding_cloud: NodeCloudProviderConfig | None
    rerank_backend: str
    rerank_model_reference: Path | None
    rerank_cloud: NodeCloudProviderConfig | None
    structured_json_backend: str
    structured_json_cloud: NodeCloudProviderConfig | None
    ollama_host: str
    llama_cpp_embedding_base_url: str
    llama_cpp_embedding_path: str
    llama_cpp_rerank_base_url: str
    llama_cpp_rerank_path: str
    model_cache_dir: Path | None
    resource_guard: NodeResourceGuard
    limits: NodeLimits

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> NodeRuntimeConfig:
        values = os.environ if environment is None else environment
        _reject_identity_override(
            values, "PP_LOCAL_NODE_PROTOCOL_VERSION", "local-inference-node/v1"
        )
        # llama.cpp is the supported local default. Ollama and in-process
        # adapters remain explicit compatibility selections and must never be
        # selected merely because a host happens to have Ollama installed.
        embedding_backend = _get(values, "PP_LOCAL_NODE_EMBEDDING_BACKEND", "llama.cpp").casefold()
        if embedding_backend not in _EMBEDDING_BACKENDS:
            raise NodeConfigurationError("node_embedding_backend_invalid")
        rerank_backend = _get(values, "PP_LOCAL_NODE_RERANK_BACKEND", "llama.cpp").casefold()
        if rerank_backend not in _RERANK_BACKENDS:
            raise NodeConfigurationError("node_rerank_backend_invalid")
        structured_json_backend = _get(
            values,
            "PP_LOCAL_NODE_STRUCTURED_JSON_BACKEND",
            "off",
        ).casefold()
        if structured_json_backend not in _STRUCTURED_JSON_BACKENDS:
            raise NodeConfigurationError("node_structured_json_backend_invalid")

        provider_class = _provider_class(
            embedding_backend=embedding_backend,
            rerank_backend=rerank_backend,
            structured_json_backend=structured_json_backend,
        )
        requested_provider_mode = _get(
            values,
            "PP_LOCAL_NODE_PROVIDER_MODE",
            provider_class,
        ).casefold()
        if requested_provider_mode not in {"local", "cloud", "hybrid"}:
            raise NodeConfigurationError("node_provider_mode_invalid")
        if requested_provider_mode != provider_class:
            raise NodeConfigurationError("node_provider_mode_backend_mismatch")
        supplied_provider_class = _get(values, "PP_LOCAL_NODE_PROVIDER_CLASS", provider_class)
        if supplied_provider_class != provider_class:
            raise NodeConfigurationError("node_provider_class_mismatch")

        embedding_revision = _required(values, "PP_LOCAL_NODE_EMBEDDING_REVISION")
        rerank_revision = _required(values, "PP_LOCAL_NODE_RERANK_REVISION")
        embedding_normalization = _required(values, "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION")
        if embedding_normalization not in _EMBEDDING_NORMALIZATIONS:
            raise NodeConfigurationError("node_embedding_normalization_unsupported")
        identity = NodeIdentity(
            protocol_version="local-inference-node/v1",
            node_id=_get(values, "PP_LOCAL_NODE_ID", "inference-node"),
            embedding_model=_required(values, "PP_LOCAL_NODE_EMBEDDING_MODEL"),
            embedding_revision=embedding_revision,
            embedding_dimension=_required_integer(values, "PP_LOCAL_NODE_EMBEDDING_DIMENSION"),
            embedding_normalization=embedding_normalization,
            rerank_model=_required(values, "PP_LOCAL_NODE_RERANK_MODEL"),
            rerank_revision=rerank_revision,
            provider_class=provider_class,
            structured_json_model=(
                _required(values, "PP_LOCAL_NODE_STRUCTURED_JSON_MODEL")
                if structured_json_backend != "off"
                else None
            ),
            structured_json_revision=(
                _required(values, "PP_LOCAL_NODE_STRUCTURED_JSON_REVISION")
                if structured_json_backend != "off"
                else None
            ),
        )
        bind_host = validate_loopback_bind_host(
            _get(values, "PP_LOCAL_NODE_BIND_HOST", "127.0.0.1")
        )
        port = validate_node_port(_integer(values, "PP_LOCAL_NODE_PORT", 19130))
        cache_root = _get(values, "PP_LOCAL_NODE_MODEL_CACHE_DIR", "").strip()
        limits = NodeLimits(
            max_request_bytes=_integer(values, "PP_LOCAL_NODE_MAX_REQUEST_BYTES", 1024 * 1024),
            max_embedding_inputs=_integer(values, "PP_LOCAL_NODE_MAX_EMBEDDING_INPUTS", 64),
            max_embedding_input_chars=_integer(values, "PP_LOCAL_NODE_MAX_EMBEDDING_CHARS", 12_000),
            max_rerank_documents=_integer(values, "PP_LOCAL_NODE_MAX_RERANK_DOCUMENTS", 128),
            max_rerank_query_chars=_integer(values, "PP_LOCAL_NODE_MAX_RERANK_QUERY_CHARS", 4_000),
            max_rerank_document_chars=_integer(
                values, "PP_LOCAL_NODE_MAX_RERANK_DOCUMENT_CHARS", 12_000
            ),
            max_structured_system_prompt_bytes=_integer(
                values,
                "PP_LOCAL_NODE_MAX_STRUCTURED_SYSTEM_PROMPT_BYTES",
                32 * 1024,
            ),
            max_structured_user_payload_bytes=_integer(
                values,
                "PP_LOCAL_NODE_MAX_STRUCTURED_USER_PAYLOAD_BYTES",
                256 * 1024,
            ),
            max_structured_output_bytes=_integer(
                values,
                "PP_LOCAL_NODE_MAX_STRUCTURED_OUTPUT_BYTES",
                256 * 1024,
            ),
            max_structured_tokens=_integer(
                values,
                "PP_LOCAL_NODE_MAX_STRUCTURED_TOKENS",
                UNBOUNDED_STRUCTURED_TOKEN_LIMIT,
            ),
        )
        return cls(
            identity=identity,
            bind_host=bind_host,
            port=port,
            max_concurrency=_bounded_positive_integer(
                values, "PP_LOCAL_NODE_MAX_CONCURRENCY", default=1, maximum=64
            ),
            embedding_backend=embedding_backend,
            embedding_model_reference=(
                None
                if embedding_backend in {"cloud", "openai-compatible"}
                else _local_model_reference(
                    values,
                    "PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE",
                    "/models/embedding",
                )
            ),
            embedding_cloud=(
                _cloud_provider_config(
                    values,
                    prefix="PP_LOCAL_NODE_EMBEDDING",
                    default_path="/embeddings",
                    send_dimensions=True,
                )
                if embedding_backend in {"cloud", "openai-compatible"}
                else None
            ),
            rerank_backend=rerank_backend,
            rerank_model_reference=(
                None
                if rerank_backend in {"cloud", "openai-compatible"}
                else _local_model_reference(
                    values,
                    "PP_LOCAL_NODE_RERANK_MODEL_REFERENCE",
                    "/models/rerank",
                )
            ),
            rerank_cloud=(
                _cloud_provider_config(
                    values,
                    prefix="PP_LOCAL_NODE_RERANK",
                    default_path="/rerank",
                    send_dimensions=False,
                )
                if rerank_backend in {"cloud", "openai-compatible"}
                else None
            ),
            structured_json_backend=structured_json_backend,
            structured_json_cloud=(
                _cloud_provider_config(
                    values,
                    prefix="PP_LOCAL_NODE_STRUCTURED_JSON",
                    default_path="/chat/completions",
                    send_dimensions=False,
                )
                if structured_json_backend in {"cloud", "openai-compatible"}
                else None
            ),
            ollama_host=validate_local_ollama_host(
                _get(values, "PP_LOCAL_NODE_OLLAMA_HOST", "http://127.0.0.1:11434")
            ),
            llama_cpp_embedding_base_url=validate_local_llama_cpp_url(
                _get(values, "PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL", "http://127.0.0.1:19131")
            ),
            llama_cpp_embedding_path=_path(
                values, "PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH", "/v1/embeddings"
            ),
            llama_cpp_rerank_base_url=validate_local_llama_cpp_url(
                _get(values, "PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL", "http://127.0.0.1:19132")
            ),
            llama_cpp_rerank_path=_path(
                values, "PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH", "/rerank"
            ),
            model_cache_dir=Path(cache_root) if cache_root else None,
            resource_guard=resource_guard_from_environment(values),
            limits=limits,
        )


def create_embedding_engine(
    config: NodeRuntimeConfig,
    *,
    embedding_artifact_sha256: str | None = None,
) -> EmbeddingEngine:
    """Build the selected local engine without silently changing its identity."""

    if config.embedding_backend in {"cloud", "openai-compatible"}:
        cloud = _required_cloud_config(config.embedding_cloud, "node_cloud_embedding_config_missing")
        return CloudEmbeddingAdapter(
            api_key=cloud.api_key,
            base_url=cloud.base_url,
            path=cloud.path,
            model=config.identity.embedding_model,
            revision=config.identity.embedding_revision,
            expected_dimension=config.identity.embedding_dimension,
            normalization=config.identity.embedding_normalization,
            send_dimensions=cloud.send_dimensions,
        )
    if config.embedding_backend == "llama.cpp":
        return LlamaCppEmbeddingAdapter(
            base_url=config.llama_cpp_embedding_base_url,
            path=config.llama_cpp_embedding_path,
            model=config.identity.embedding_model,
            expected_dimension=config.identity.embedding_dimension,
            normalization=config.identity.embedding_normalization,
        )
    if config.embedding_backend == "ollama":
        expected_artifact = embedding_artifact_sha256 or _ollama_model_digest(
            config.ollama_host,
            config.identity.embedding_model,
        )
        return OllamaEmbeddingAdapter(
            host=config.ollama_host,
            model=config.identity.embedding_model,
            expected_dimension=config.identity.embedding_dimension,
            expected_artifact_sha256=expected_artifact,
            identity_probe=lambda: _ollama_model_digest(
                config.ollama_host,
                config.identity.embedding_model,
            ),
            normalization=config.identity.embedding_normalization,
        )
    model_reference = _required_local_reference(
        config.embedding_model_reference,
        "node_embedding_model_reference_missing",
    )
    return SentenceTransformersEmbeddingAdapter(
        model_reference=str(model_reference),
        revision=config.identity.embedding_revision,
        cache_dir=config.model_cache_dir,
        normalization=config.identity.embedding_normalization,
    )


def create_reranking_engine(config: NodeRuntimeConfig) -> RerankingEngine:
    """Build the declared local reranker; unsupported backends fail closed above."""

    if config.rerank_backend in {"cloud", "openai-compatible"}:
        cloud = _required_cloud_config(config.rerank_cloud, "node_cloud_rerank_config_missing")
        return CloudRerankingAdapter(
            api_key=cloud.api_key,
            base_url=cloud.base_url,
            path=cloud.path,
            model=config.identity.rerank_model,
            revision=config.identity.rerank_revision,
        )
    if config.rerank_backend == "llama.cpp":
        return LlamaCppRerankingAdapter(
            base_url=config.llama_cpp_rerank_base_url,
            path=config.llama_cpp_rerank_path,
            model=config.identity.rerank_model,
        )
    model_reference = _required_local_reference(
        config.rerank_model_reference,
        "node_rerank_model_reference_missing",
    )
    if config.rerank_backend == "qwen3-cross-encoder":
        return Qwen3CrossEncoderReranker(
            model_reference=str(model_reference),
            revision=config.identity.rerank_revision,
            cache_dir=config.model_cache_dir,
        )
    return LocalBgeReranker(
        model_reference=str(model_reference),
        revision=config.identity.rerank_revision,
        cache_dir=config.model_cache_dir,
    )


def create_structured_json_engine(config: NodeRuntimeConfig) -> StructuredJSONEngine | None:
    """Build the optional node-owned structured JSON engine."""

    if config.structured_json_backend == "off":
        return None
    cloud = _required_cloud_config(
        config.structured_json_cloud,
        "node_cloud_structured_config_missing",
    )
    model = config.identity.structured_json_model
    if model is None:
        raise NodeConfigurationError("node_structured_json_identity_missing")
    return CloudStructuredJSONAdapter(
        api_key=cloud.api_key,
        base_url=cloud.base_url,
        path=cloud.path,
        model=model,
        revision=config.identity.structured_json_revision,
        max_system_prompt_bytes=config.limits.max_structured_system_prompt_bytes,
        max_user_payload_bytes=config.limits.max_structured_user_payload_bytes,
        max_output_bytes=config.limits.max_structured_output_bytes,
        max_tokens=config.limits.max_structured_tokens,
    )


def validate_loopback_bind_host(host: str) -> str:
    """Reject any listener that could expose the node to LAN or public traffic."""

    if host not in {"127.0.0.1", "::1"}:
        raise NodeConfigurationError("node_bind_host_must_be_loopback")
    return host


def validate_node_port(port: int) -> int:
    if isinstance(port, bool) or not 1024 <= port <= 65535:
        raise NodeConfigurationError("node_port_invalid")
    return port


def validate_local_ollama_host(value: str) -> str:
    """Keep the optional Ollama adapter local to this host/network namespace."""

    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "host.docker.internal"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise NodeConfigurationError("node_ollama_host_not_allowed")
    return value.rstrip("/")


def validate_local_llama_cpp_url(value: str) -> str:
    """Keep llama-server transports local to the compute-node namespace."""

    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "host.docker.internal"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise NodeConfigurationError("node_llama_cpp_host_not_allowed")
    return value.rstrip("/")


def _path(values: Mapping[str, str], name: str, default: str) -> str:
    value = _get(values, name, default)
    if not value.startswith("/") or value.startswith("//"):
        raise NodeConfigurationError(f"{name.casefold()}_invalid")
    return value


def bind_model_artifact_identity(config: NodeRuntimeConfig) -> NodeIdentity:
    """Derive model-content proof before serving, never during ``--config-check``."""

    embedding_digest: str | None = None
    if config.embedding_backend == "ollama":
        embedding_digest = _ollama_model_digest(
            config.ollama_host,
            config.identity.embedding_model,
        )
    elif config.embedding_backend in {"cloud", "openai-compatible"}:
        embedding_digest = _cloud_identity_digest(
            config.identity.embedding_model,
            config.identity.embedding_revision,
            config.identity.embedding_dimension,
            config.identity.embedding_normalization,
        )
    else:
        embedding_digest = _model_tree_sha256(
            _required_local_reference(
                config.embedding_model_reference,
                "node_embedding_model_reference_missing",
            )
        )
    rerank_digest = (
        _cloud_identity_digest(
            config.identity.rerank_model,
            config.identity.rerank_revision,
        )
        if config.rerank_backend in {"cloud", "openai-compatible"}
        else _model_tree_sha256(
            _required_local_reference(
                config.rerank_model_reference,
                "node_rerank_model_reference_missing",
            )
        )
    )
    return replace(
        config.identity,
        embedding_artifact_sha256=embedding_digest,
        rerank_artifact_sha256=rerank_digest,
    )


def _cloud_identity_digest(*parts: object) -> str:
    """Stable public proof for a node-owned cloud model, without endpoint/key."""

    material = "cloud-model\x1f" + "\x1f".join(str(part) for part in parts)
    return "sha256:" + sha256(material.encode("utf-8")).hexdigest()


def _get(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default)
    return value.strip() if isinstance(value, str) else default


def _provider_class(
    *,
    embedding_backend: str,
    rerank_backend: str,
    structured_json_backend: str,
) -> str:
    providers = {
        "cloud" if backend in {"cloud", "openai-compatible"} else "local"
        for backend in (embedding_backend, rerank_backend, structured_json_backend)
        if backend != "off"
    }
    if providers == {"cloud"}:
        return "cloud"
    if providers == {"local"} or not providers:
        return "local"
    return "hybrid"


def _cloud_provider_config(
    values: Mapping[str, str],
    *,
    prefix: str,
    default_path: str,
    send_dimensions: bool,
) -> NodeCloudProviderConfig:
    api_key = _get(values, f"{prefix}_API_KEY", "") or _required(
        values, "PP_LOCAL_NODE_CLOUD_API_KEY"
    )
    # Compose and the Control private projection use the explicit ``CLOUD``
    # infix.  Keep the older names as a compatibility fallback for existing
    # hand-written node profiles.
    base_url = (
        _get(values, f"{prefix}_CLOUD_BASE_URL", "")
        or _get(values, f"{prefix}_BASE_URL", "")
        or _required(values, "PP_LOCAL_NODE_CLOUD_BASE_URL")
    )
    path = _get(
        values,
        f"{prefix}_CLOUD_PATH",
        _get(
            values,
            f"{prefix}_PATH",
            _get(values, "PP_LOCAL_NODE_CLOUD_PATH", default_path),
        ),
    )
    return NodeCloudProviderConfig(
        api_key=api_key,
        base_url=base_url,
        path=path,
        send_dimensions=_boolean(values, f"{prefix}_SEND_DIMENSIONS", send_dimensions),
    )


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None:
        return default
    normalized = str(raw).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise NodeConfigurationError(f"{name.casefold()}_invalid")


def _required_cloud_config(
    config: NodeCloudProviderConfig | None,
    reason: str,
) -> NodeCloudProviderConfig:
    if config is None:
        raise NodeConfigurationError(reason)
    return config


def _required_local_reference(reference: Path | None, reason: str) -> Path:
    if reference is None:
        raise NodeConfigurationError(reason)
    return reference


def _required(values: Mapping[str, str], name: str) -> str:
    value = _get(values, name, "")
    if not value:
        raise NodeConfigurationError(f"{name.casefold()}_required")
    return value


def _integer(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw))
    except (TypeError, ValueError) as exc:
        raise NodeConfigurationError(f"{name.casefold()}_invalid") from exc


def _reject_identity_override(values: Mapping[str, str], name: str, expected: str) -> None:
    supplied = values.get(name)
    if supplied is not None and str(supplied).strip() != expected:
        raise NodeConfigurationError(f"{name.casefold()}_override_forbidden")


def _required_integer(values: Mapping[str, str], name: str) -> int:
    raw = _required(values, name)
    try:
        return int(raw)
    except ValueError as exc:
        raise NodeConfigurationError(f"{name.casefold()}_invalid") from exc


def _bounded_positive_integer(
    values: Mapping[str, str], name: str, *, default: int, maximum: int
) -> int:
    value = _integer(values, name, default)
    if isinstance(value, bool) or not 1 <= value <= maximum:
        raise NodeConfigurationError(f"{name.casefold()}_invalid")
    return value


def _local_model_reference(values: Mapping[str, str], name: str, default: str) -> Path:
    reference = Path(_get(values, name, default))
    if not reference.is_absolute() or ".." in reference.parts:
        raise NodeConfigurationError(f"{name.casefold()}_must_be_local_absolute_path")
    return reference


def _model_tree_sha256(root: Path) -> str:
    """Hash an immutable local model file or tree without following links."""

    if root.is_symlink():
        raise NodeConfigurationError("node_model_artifact_symlink_forbidden")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise NodeConfigurationError("node_model_artifact_missing") from exc
    if resolved_root.is_file():
        digest = sha256()
        try:
            with resolved_root.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise NodeConfigurationError("node_model_artifact_unreadable") from exc
        return f"sha256:{digest.hexdigest()}"
    if not resolved_root.is_dir():
        raise NodeConfigurationError("node_model_artifact_directory_invalid")

    digest = sha256()
    file_count = 0
    try:
        for entry in sorted(resolved_root.rglob("*")):
            if entry.is_symlink():
                raise NodeConfigurationError("node_model_artifact_symlink_forbidden")
            if entry.is_dir():
                continue
            if not entry.is_file():
                raise NodeConfigurationError("node_model_artifact_entry_invalid")
            relative_path = entry.relative_to(resolved_root).as_posix().encode("utf-8")
            digest.update(len(relative_path).to_bytes(8, "big"))
            digest.update(relative_path)
            with entry.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            file_count += 1
    except OSError as exc:
        raise NodeConfigurationError("node_model_artifact_unreadable") from exc
    if file_count == 0:
        raise NodeConfigurationError("node_model_artifact_empty")
    return f"sha256:{digest.hexdigest()}"


def _ollama_model_digest(host: str, model: str) -> str:
    """Bind an Ollama model to the digest its loopback registry currently reports."""

    try:
        with urlopen(f"{host}/api/tags", timeout=5) as response:  # noqa: S310 -- host is loopback-validated.
            payload = json.loads(response.read())
    except (OSError, ValueError, TypeError) as exc:
        raise NodeConfigurationError("node_ollama_model_identity_unavailable") from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise NodeConfigurationError("node_ollama_model_identity_invalid")
    for candidate in models:
        if not isinstance(candidate, dict) or candidate.get("name") != model:
            continue
        digest = candidate.get("digest")
        if isinstance(digest, str) and len(digest) == 64:
            return f"sha256:{digest.casefold()}"
    raise NodeConfigurationError("node_ollama_model_digest_missing")
