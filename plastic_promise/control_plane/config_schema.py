"""Strict, secret-aware schema for remotely managed cloud configuration.

The public payload is deliberately not an environment-variable bag.  It has
four fixed sections and is rendered to a fixed environment allowlist only
after validation.  Secret operations travel on a separate write-only channel
and are never included in public results.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

CONFIG_CONTRACT = "control-plane-config/v1"

_TOP_LEVEL_FIELDS = frozenset({"embedding", "rerank", "chunk_inference", "gateway", "node_routing"})
_SECRET_ENV_NAMES = MappingProxyType(
    {
        "embedding_api_key": "EMBEDDER_API_KEY",
        "rerank_api_key": "PP_RERANK_API_KEY",
        "chunk_inference_api_key": "PP_MEMORY_CHUNK_ENRICHMENT_API_KEY",
        # The value is write-only in the control plane and is projected only
        # to the compute-node runtime by the deployment supervisor.
        "compute_node_cloud_api_key": "PP_LOCAL_NODE_CLOUD_API_KEY",
        "gateway_token": "PP_INFERENCE_GATEWAY_TOKEN",
    }
)
BOOTSTRAP_ONLY_ENV_NAMES = frozenset(
    {
        "EMBEDDER_CHUNK_CHARS",
        "EMBEDDER_STRUCTURE_HARD_CHARS",
        "EMBEDDER_STRUCTURE_MAX_CHUNKS",
        "EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS",
        "PLASTIC_DB_PATH",
        "PLASTIC_LANCEDB_GENERATION_ROOT",
        "PLASTIC_LANCEDB_LIVE_ROOT",
        "PLASTIC_LANCEDB_PATH",
        "PLASTIC_PROJECT_ID",
        "PP_CONTROL_OPERATOR_TOKEN_SHA256",
        "PP_CONTROL_ALLOWED_ORIGINS",
        "PP_CONTROL_PLANE",
        "PP_CONTROL_ROOT",
        "PP_CONTROL_SECRET_ADMIN_TOKEN_SHA256",
        "PP_CONTROL_VIEWER_TOKEN_SHA256",
        "PP_INFERENCE_CLIENT_VECTOR_DIMENSION",
        "PP_INFERENCE_CLIENT_VECTOR_IDENTITY",
        "PP_INFERENCE_GATEWAY_BIND",
        "PP_INFERENCE_GATEWAY_DB_PATH",
        "PP_INFERENCE_GATEWAY_PROJECT_ID",
        "PP_INFERENCE_GATEWAY_TOKEN",
        "PP_INFERENCE_PROVIDER_HOST_ALLOWLIST",
        "PP_MAINTENANCE_ENABLED",
        "PP_MAINTENANCE_RUN_DIR",
        "PP_PROJECT_ID",
    }
)
_REMOTE_SECRET_NAMES = frozenset(
    {
        "embedding_api_key",
        "rerank_api_key",
        "chunk_inference_api_key",
        "compute_node_cloud_api_key",
    }
)
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_PROJECT_RE = re.compile(r"project:[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_SAFE_SECRET_RE = re.compile(r"[A-Za-z0-9._~+/=:@%-]{8,4096}\Z")
_GATEWAY_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,512}\Z")
_NODE_ID_RE = re.compile(r"[a-z][a-z0-9_.:-]{1,127}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DOCUMENTATION_HOST_LABELS = frozenset({"doc", "docs", "documentation", "wiki"})

_EMBEDDING_FIELDS = frozenset(
    {
        "enabled",
        "provider",
        "base_url",
        "path",
        "model",
        "model_revision",
        "dimension",
        "send_dimensions",
        "batch_size",
        "max_input_bytes",
        "max_total_input_bytes",
        "max_request_bytes",
        "max_response_bytes",
        "timeout_seconds",
        "total_timeout_seconds",
        "max_retries",
        "cache_size",
        "cache_ttl_seconds",
        "cost_per_million_tokens",
        "cost_currency",
        "pricing_revision",
    }
)
_RERANK_FIELDS = frozenset(
    {
        "enabled",
        "providers",
        "base_url",
        "path",
        "model",
        "model_revision",
        "timeout_seconds",
        "total_timeout_seconds",
        "max_retries",
        "max_candidates",
        "max_document_chars",
        "max_query_chars",
    }
)
_CHUNK_INFERENCE_FIELDS = frozenset(
    {
        "chunking_mode",
        "enrichment_mode",
        "provider",
        "base_url",
        "path",
        "model",
        "model_revision",
        "timeout_seconds",
        "temperature",
        "top_p",
        "json_mode",
        "num_predict",
        "max_output_chars",
        "queue_size",
        "worker_idle_timeout_seconds",
        "fusion_mode",
        "fusion_batch_size",
        "fusion_max_wait_seconds",
        "fusion_max_queue_size",
        "fusion_workers",
        "fusion_lease_seconds",
        "fusion_retry_delay_seconds",
        "fusion_poll_seconds",
    }
)
_GATEWAY_FIELDS = frozenset(
    {
        "enabled",
        "project_id",
        "ttl_seconds",
        "lease_seconds",
        "max_concurrency",
        "max_active_jobs",
        "retention_seconds",
        "max_retained_rows",
        "max_retained_json_bytes",
        "provider_host_allowlist",
    }
)
_NODE_ROUTING_FIELDS = frozenset(
    {
        "enabled",
        "inference_mode",
        "embedding_policy",
        "rerank_policy",
        "structured_json_policy",
        "embedding_required_identity",
        "rerank_required_identity",
        "structured_json_required_identity",
        "embedding_pinned_node_id",
        "rerank_pinned_node_id",
        "structured_json_pinned_node_id",
        "allowed_node_ids",
        "project_overrides",
        "accelerator_max_enabled",
        "accelerator_max_concurrency",
        "accelerator_max_queue_depth",
        "accelerator_max_daily_tasks",
        "accelerator_min_free_memory_mib",
    }
)
_SECTION_FIELDS = {
    "embedding": _EMBEDDING_FIELDS,
    "rerank": _RERANK_FIELDS,
    "chunk_inference": _CHUNK_INFERENCE_FIELDS,
    "gateway": _GATEWAY_FIELDS,
    "node_routing": _NODE_ROUTING_FIELDS,
}
_SECTION_PATCH_FIELDS = {
    **_SECTION_FIELDS,
    "gateway": _GATEWAY_FIELDS - {"project_id", "provider_host_allowlist"},
}


class ControlPlaneError(RuntimeError):
    """Base error with an HTTP-safe stable code."""

    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class ControlPlaneValidationError(ControlPlaneError):
    """A candidate configuration failed the public schema."""

    def __init__(self, code: str) -> None:
        super().__init__(code, status_code=400)


@dataclass(frozen=True)
class PreparedConfiguration:
    """Internal normalized configuration, including write-only material."""

    safe_config: Mapping[str, object]
    secret_state: Mapping[str, bool]
    embedding_identity: str
    environment: Mapping[str, str] = field(repr=False)
    secret_values: Mapping[str, str] = field(repr=False)
    secret_operations: Mapping[str, str] = field(repr=False)
    compute_environment: Mapping[str, str] = field(default_factory=dict, repr=False)


def default_safe_config() -> dict[str, object]:
    """Return a new fail-closed cloud configuration."""

    return {
        "embedding": {
            "enabled": False,
            "provider": "openai-compatible",
            "base_url": "",
            "path": "/embeddings",
            "model": "text-embedding-v4",
            "model_revision": "text-embedding-v4",
            "dimension": 1024,
            "send_dimensions": True,
            "batch_size": 32,
            "max_input_bytes": 64 * 1024,
            "max_total_input_bytes": 1024 * 1024,
            "max_request_bytes": 2 * 1024 * 1024,
            "max_response_bytes": 16 * 1024 * 1024,
            "timeout_seconds": 15.0,
            "total_timeout_seconds": 60.0,
            "max_retries": 3,
            "cache_size": 256,
            "cache_ttl_seconds": 300,
            "cost_per_million_tokens": None,
            "cost_currency": "",
            "pricing_revision": "",
        },
        "rerank": {
            "enabled": False,
            "providers": ["cloud", "original"],
            "base_url": "",
            "path": "/rerank",
            "model": "",
            "model_revision": "",
            "timeout_seconds": 5.0,
            "total_timeout_seconds": 10.0,
            "max_retries": 2,
            "max_candidates": 30,
            "max_document_chars": 4_000,
            "max_query_chars": 4_000,
        },
        "chunk_inference": {
            "chunking_mode": "off",
            "enrichment_mode": "off",
            "provider": "openai-compatible",
            "base_url": "",
            "path": "/chat/completions",
            "model": "",
            "model_revision": "",
            "timeout_seconds": 45.0,
            "temperature": 0.0,
            "top_p": 1.0,
            "json_mode": True,
            "num_predict": 768,
            "max_output_chars": 8192,
            "queue_size": 32,
            "worker_idle_timeout_seconds": 30.0,
            "fusion_mode": "off",
            "fusion_batch_size": 20,
            "fusion_max_wait_seconds": 2.0,
            "fusion_max_queue_size": 1_000,
            "fusion_workers": 2,
            "fusion_lease_seconds": 120,
            "fusion_retry_delay_seconds": 5,
            "fusion_poll_seconds": 0.25,
        },
        "gateway": {
            "enabled": False,
            "project_id": "",
            "ttl_seconds": 900,
            "lease_seconds": 120,
            "max_concurrency": 4,
            "max_active_jobs": 1_000,
            "retention_seconds": 86_400,
            "max_retained_rows": None,
            "max_retained_json_bytes": 512 * 1024 * 1024,
            "provider_host_allowlist": [],
        },
        "node_routing": {
            "enabled": False,
            "inference_mode": "local",
            "embedding_policy": "remote-node-first",
            "rerank_policy": "remote-node-first",
            "structured_json_policy": "remote-node-first",
            "embedding_required_identity": "",
            "rerank_required_identity": "",
            "structured_json_required_identity": "",
            "embedding_pinned_node_id": "",
            "rerank_pinned_node_id": "",
            "structured_json_pinned_node_id": "",
            "allowed_node_ids": [],
            "project_overrides": {},
            "accelerator_max_enabled": False,
            "accelerator_max_concurrency": 1,
            "accelerator_max_queue_depth": 32,
            "accelerator_max_daily_tasks": 100,
            "accelerator_min_free_memory_mib": 1024,
        },
    }


def safe_config_from_environment(environ: Mapping[str, object]) -> dict[str, object]:
    """Project the managed environment allowlist into a public configuration."""

    config = default_safe_config()
    embedding = config["embedding"]
    rerank = config["rerank"]
    chunk = config["chunk_inference"]
    gateway = config["gateway"]
    node_routing = config["node_routing"]
    assert isinstance(embedding, dict)
    assert isinstance(rerank, dict)
    assert isinstance(chunk, dict)
    assert isinstance(gateway, dict)
    assert isinstance(node_routing, dict)

    embedding_provider = _env_text(environ, "EMBEDDER_PROVIDER", "").casefold()
    embedding["enabled"] = embedding_provider in {
        "cloud",
        "openai",
        "openai-compatible",
    }
    embedding.update(
        {
            "base_url": _env_text(environ, "EMBEDDER_BASE_URL", ""),
            "path": _env_text(environ, "EMBEDDER_PATH", "/embeddings"),
            "model": _env_text(environ, "EMBEDDER_MODEL", "text-embedding-v4"),
            "model_revision": _env_text(
                environ,
                "EMBEDDER_MODEL_REVISION",
                _env_text(environ, "EMBEDDER_MODEL", "text-embedding-v4"),
            ),
            "dimension": _env_int(
                environ,
                ("PP_EMBEDDING_DIM", "EMBEDDER_DIMENSION"),
                1024,
            ),
            "send_dimensions": _env_bool(environ, "EMBEDDER_SEND_DIMENSIONS", True),
            "batch_size": _env_int(environ, ("EMBEDDER_BATCH_SIZE",), 32),
            "max_input_bytes": _env_int(environ, ("EMBEDDER_MAX_INPUT_BYTES",), 64 * 1024),
            "max_total_input_bytes": _env_int(
                environ,
                ("EMBEDDER_MAX_TOTAL_INPUT_BYTES",),
                1024 * 1024,
            ),
            "max_request_bytes": _env_int(
                environ,
                ("EMBEDDER_MAX_REQUEST_BYTES",),
                2 * 1024 * 1024,
            ),
            "max_response_bytes": _env_int(
                environ,
                ("EMBEDDER_MAX_RESPONSE_BYTES",),
                16 * 1024 * 1024,
            ),
            "timeout_seconds": _env_float(environ, ("EMBEDDER_TIMEOUT",), 15.0),
            "total_timeout_seconds": _env_float(
                environ,
                ("EMBEDDER_TOTAL_TIMEOUT",),
                60.0,
            ),
            "max_retries": _env_int(environ, ("EMBEDDER_MAX_RETRIES",), 3),
            "cache_size": _env_int(environ, ("EMBEDDER_CACHE_SIZE",), 256),
            "cache_ttl_seconds": _env_int(environ, ("EMBEDDER_CACHE_TTL",), 300),
            "cost_per_million_tokens": _env_optional_float(
                environ,
                ("EMBEDDER_COST_PER_MILLION_TOKENS",),
            ),
            "cost_currency": _env_text(environ, "EMBEDDER_COST_CURRENCY", "").upper(),
            "pricing_revision": _env_text(environ, "EMBEDDER_PRICING_REVISION", ""),
        }
    )

    raw_providers = [
        _canonical_rerank_provider(value)
        for value in _env_text(environ, "PP_RERANK_PROVIDERS", "").split(",")
        if value.strip()
    ]
    cloud_chain = bool(raw_providers and raw_providers[0] == "cloud")
    rerank["enabled"] = _env_text(environ, "PP_RERANK_DISABLED", "0") != "1" and cloud_chain
    rerank.update(
        {
            "providers": raw_providers if cloud_chain else ["cloud", "original"],
            "base_url": _env_text(environ, "PP_RERANK_BASE_URL", ""),
            "path": _env_text(environ, "PP_RERANK_PATH", "/rerank"),
            "model": _env_text(
                environ,
                "PP_RERANK_CLOUD_MODEL",
                _env_text(environ, "PP_RERANK_MODEL", ""),
            ),
            "model_revision": _env_text(
                environ,
                "PP_RERANK_CLOUD_MODEL_REVISION",
                _env_text(environ, "PP_RERANK_MODEL_REVISION", ""),
            ),
            "timeout_seconds": _env_float(
                environ,
                ("PP_RERANK_TIMEOUT_SEC", "PP_RERANK_TIMEOUT"),
                5.0,
            ),
            "total_timeout_seconds": _env_float(
                environ,
                ("PP_RERANK_TOTAL_TIMEOUT_SEC", "PP_RERANK_TOTAL_TIMEOUT"),
                10.0,
            ),
            "max_retries": _env_int(environ, ("PP_RERANK_MAX_RETRIES",), 2),
            "max_candidates": _env_int(environ, ("PP_RERANK_MAX_CANDIDATES",), 30),
            "max_document_chars": _env_int(
                environ,
                ("PP_RERANK_MAX_DOCUMENT_CHARS",),
                4_000,
            ),
            "max_query_chars": _env_int(
                environ,
                ("PP_RERANK_MAX_QUERY_CHARS",),
                4_000,
            ),
        }
    )

    chunk.update(
        {
            "chunking_mode": _env_text(environ, "PP_MEMORY_CHUNKING", "off").casefold(),
            "enrichment_mode": _env_text(
                environ,
                "PP_MEMORY_CHUNK_ENRICHMENT",
                "off",
            ).casefold(),
            "provider": "openai-compatible",
            "base_url": _env_text(environ, "PP_MEMORY_CHUNK_ENRICHMENT_BASE_URL", ""),
            "path": _env_text(
                environ,
                "PP_MEMORY_CHUNK_ENRICHMENT_PATH",
                "/chat/completions",
            ),
            "model": _env_text(environ, "PP_MEMORY_CHUNK_ENRICHMENT_MODEL", ""),
            "model_revision": _env_text(
                environ,
                "PP_MEMORY_CHUNK_ENRICHMENT_MODEL_REVISION",
                "",
            ),
            "timeout_seconds": _env_float(
                environ,
                ("PP_MEMORY_CHUNK_ENRICHMENT_TIMEOUT",),
                45.0,
            ),
            "temperature": _env_float(
                environ,
                ("PP_MEMORY_CHUNK_ENRICHMENT_TEMPERATURE",),
                0.0,
            ),
            "top_p": _env_float(
                environ,
                ("PP_MEMORY_CHUNK_ENRICHMENT_TOP_P",),
                1.0,
            ),
            "json_mode": _env_bool(
                environ,
                "PP_MEMORY_CHUNK_ENRICHMENT_JSON_MODE",
                True,
            ),
            "num_predict": _env_int(
                environ,
                ("PP_MEMORY_CHUNK_ENRICHMENT_NUM_PREDICT",),
                768,
            ),
            "max_output_chars": _env_int(
                environ,
                ("PP_MEMORY_CHUNK_ENRICHMENT_MAX_OUTPUT_CHARS",),
                8192,
            ),
            "queue_size": _env_int(
                environ,
                ("PP_MEMORY_CHUNK_ENRICHMENT_QUEUE_SIZE",),
                32,
            ),
            "worker_idle_timeout_seconds": _env_float(
                environ,
                ("PP_MEMORY_CHUNK_ENRICHMENT_WORKER_IDLE_TIMEOUT",),
                30.0,
            ),
            "fusion_mode": _env_text(
                environ,
                "PP_STRUCTURED_MEMORY_FUSION",
                "off",
            ).casefold(),
            "fusion_batch_size": _env_int(
                environ,
                ("PP_STRUCTURED_MEMORY_FUSION_BATCH_SIZE",),
                20,
            ),
            "fusion_max_wait_seconds": _env_float(
                environ,
                ("PP_STRUCTURED_MEMORY_FUSION_MAX_WAIT_SECONDS",),
                2.0,
            ),
            "fusion_max_queue_size": _env_int(
                environ,
                ("PP_STRUCTURED_MEMORY_FUSION_MAX_QUEUE",),
                1_000,
            ),
            "fusion_workers": _env_int(
                environ,
                ("PP_STRUCTURED_MEMORY_FUSION_WORKERS",),
                2,
            ),
            "fusion_lease_seconds": _env_int(
                environ,
                ("PP_STRUCTURED_MEMORY_FUSION_LEASE_SECONDS",),
                120,
            ),
            "fusion_retry_delay_seconds": _env_int(
                environ,
                ("PP_STRUCTURED_MEMORY_FUSION_RETRY_DELAY_SECONDS",),
                5,
            ),
            "fusion_poll_seconds": _env_float(
                environ,
                ("PP_STRUCTURED_MEMORY_FUSION_POLL_SECONDS",),
                0.25,
            ),
        }
    )

    project = _env_text(environ, "PP_INFERENCE_GATEWAY_PROJECT_ID", "")
    if project and not project.startswith("project:"):
        project = f"project:{project}"
    raw_rows = _env_text(environ, "PP_INFERENCE_GATEWAY_MAX_RETAINED_ROWS", "")
    gateway.update(
        {
            "enabled": _env_text(environ, "PP_INFERENCE_GATEWAY", "0") == "1",
            "project_id": project,
            "ttl_seconds": _env_int(environ, ("PP_INFERENCE_GATEWAY_TTL_SEC",), 900),
            "lease_seconds": _env_int(environ, ("PP_INFERENCE_GATEWAY_LEASE_SEC",), 120),
            "max_concurrency": _env_int(
                environ,
                ("PP_INFERENCE_GATEWAY_MAX_CONCURRENCY",),
                4,
            ),
            "max_active_jobs": _env_int(
                environ,
                ("PP_INFERENCE_GATEWAY_MAX_ACTIVE_JOBS",),
                1_000,
            ),
            "retention_seconds": _env_int(
                environ,
                ("PP_INFERENCE_GATEWAY_RETENTION_SEC",),
                86_400,
            ),
            "max_retained_rows": int(raw_rows) if raw_rows.isdigit() else None,
            "max_retained_json_bytes": _env_int(
                environ,
                ("PP_INFERENCE_GATEWAY_MAX_RETAINED_JSON_BYTES",),
                512 * 1024 * 1024,
            ),
            "provider_host_allowlist": [
                host.strip().casefold()
                for host in _env_text(
                    environ,
                    "PP_INFERENCE_PROVIDER_HOST_ALLOWLIST",
                    "",
                ).split(",")
                if host.strip()
            ],
        }
    )
    return config


def secret_values_from_environment(environ: Mapping[str, object]) -> dict[str, str]:
    """Read only the fixed secret allowlist from a trusted process environment."""

    return {
        public_name: str(environ.get(env_name) or "")
        for public_name, env_name in _SECRET_ENV_NAMES.items()
    }


def secret_state(secret_values: Mapping[str, str]) -> dict[str, bool]:
    """Return public presence flags without exposing values or fingerprints."""

    return {name: bool(secret_values.get(name)) for name in _SECRET_ENV_NAMES}


def prepare_configuration(
    current_config: Mapping[str, object],
    current_secret_values: Mapping[str, str],
    candidate: Mapping[str, object],
    secret_ops: Mapping[str, object] | None,
) -> PreparedConfiguration:
    """Merge and validate a strict candidate patch and write-only secret operations."""

    current = _normalize_config(current_config)
    patch = _strict_mapping(candidate, reason="control_config_mapping_required")
    _reject_unknown(patch, _TOP_LEVEL_FIELDS)
    merged = deepcopy(current)
    for section_name, raw_section in patch.items():
        section = _strict_mapping(raw_section, reason="control_config_section_mapping_required")
        _reject_unknown(section, _SECTION_PATCH_FIELDS[section_name])
        target = merged[section_name]
        assert isinstance(target, dict)
        target.update(section)

    normalized = _normalize_config(merged)
    operations = _normalize_secret_operations(secret_ops or {})
    values = {name: str(current_secret_values.get(name) or "") for name in _SECRET_ENV_NAMES}
    for name, operation in operations.items():
        if operation["op"] == "clear":
            values[name] = ""
        else:
            values[name] = operation["value"]

    _validate_required_secrets(normalized, values)
    environment = _render_environment(normalized, values)
    compute_environment = _render_compute_environment(normalized, values)
    return PreparedConfiguration(
        safe_config=normalized,
        secret_state=secret_state(values),
        embedding_identity=embedding_identity(normalized),
        environment=environment,
        compute_environment=compute_environment,
        secret_values=values,
        secret_operations={name: operation["op"] for name, operation in operations.items()},
    )


def normalize_safe_config(config: Mapping[str, object]) -> dict[str, object]:
    """Validate and upgrade one public config without requiring secret values."""

    return _normalize_config(config)


def routing_for_project(config: Mapping[str, object], project_id: str) -> dict[str, object]:
    """Return a detached project-scoped routing view from an active config.

    The base node policy remains the default; an optional normalized overlay
    can change only hot-routable mode/policy fields for one project.  No
    credentials or provider payloads are accepted in this projection.
    """

    if not isinstance(config, Mapping) or not isinstance(project_id, str):
        raise ControlPlaneValidationError("control_node_routing_project_invalid")
    routing = config.get("node_routing")
    if not isinstance(routing, Mapping):
        raise ControlPlaneValidationError("control_node_routing_unavailable")
    result = deepcopy(dict(routing))
    overlays = result.pop("project_overrides", {})
    if isinstance(overlays, Mapping):
        overlay = overlays.get(project_id)
        if isinstance(overlay, Mapping):
            for name in (
                "inference_mode",
                "embedding_policy",
                "rerank_policy",
                "structured_json_policy",
            ):
                if name in overlay:
                    result[name] = overlay[name]
    return result


def embedding_identity(config: Mapping[str, object]) -> str:
    """Hash every non-secret setting that can change derived embedding material."""

    normalized = _normalize_config(config)
    embedding = normalized["embedding"]
    chunk = normalized["chunk_inference"]
    assert isinstance(embedding, dict)
    assert isinstance(chunk, dict)
    if not embedding["enabled"]:
        payload: dict[str, object] = {"enabled": False}
    else:
        effective_chunking = "structure-v1" if chunk["chunking_mode"] == "structure-v1" else "off"
        payload = {
            "enabled": True,
            "provider": embedding["provider"],
            "base_url": embedding["base_url"],
            "path": embedding["path"],
            "model": embedding["model"],
            "model_revision": embedding["model_revision"],
            "dimension": embedding["dimension"],
            "send_dimensions": embedding["send_dimensions"],
            "chunking_mode": effective_chunking,
        }
        if chunk["enrichment_mode"] == "on":
            payload["chunk_enrichment"] = {
                "provider": chunk["provider"],
                "base_url": chunk["base_url"],
                "path": chunk["path"],
                "model": chunk["model"],
                "model_revision": chunk["model_revision"],
                "temperature": chunk["temperature"],
                "top_p": chunk["top_p"],
                "json_mode": chunk["json_mode"],
            }
    return _canonical_sha256(payload)


def bootstrap_boundary_sha256(environ: Mapping[str, object]) -> str:
    """Hash every process-visible bootstrap input frozen by a staged revision.

    The digest is private revision metadata. Secret values participate in the
    hash but are never persisted directly or returned by the public API.
    """

    snapshot = {
        name: (str(environ[name]) if name in environ else None)
        for name in sorted(BOOTSTRAP_ONLY_ENV_NAMES)
    }
    return _canonical_sha256(
        {
            "contract": "control-plane-bootstrap/v1",
            "environment": snapshot,
        }
    )


def runtime_embedding_index_identity(
    config: Mapping[str, object],
    environ: Mapping[str, object] | None = None,
) -> str:
    """Return the exact non-secret identity that owns derived vectors.

    This mirrors ``memory_index.effective_embedding_model_name`` without
    mutating process-wide environment state. Bootstrap-only chunk budgets are
    included because changing any of them changes the indexed material.
    """

    normalized = _normalize_config(config)
    embedding = normalized["embedding"]
    chunk = normalized["chunk_inference"]
    node_routing = normalized["node_routing"]
    assert isinstance(embedding, dict)
    assert isinstance(chunk, dict)
    assert isinstance(node_routing, dict)

    # A governed compute node, rather than the server-only managed projection,
    # owns the vectors.  Its signed identity is already the complete
    # non-secret model/revision/dimension/normalization contract and is the
    # value persisted into generation evidence.  Chunk/material changes remain
    # bound separately by the source index-material digest.
    if node_routing["enabled"] is True:
        identity = node_routing["embedding_required_identity"]
        if not isinstance(identity, str) or not identity.startswith("sha256:"):
            raise ControlPlaneValidationError("control_node_routing_identity_required")
        return identity
    dimension = int(embedding["dimension"])
    if embedding["enabled"]:
        endpoint = hashlib.sha256(
            f"{embedding['base_url']}\0{embedding['path']}".encode()
        ).hexdigest()
        dimensions_mode = "" if embedding["send_dimensions"] else "|dimensions=native"
        identity = (
            f"{embedding['model']}|provider=openai-compatible"
            f"|revision={embedding['model_revision']}|dim={dimension}"
            f"|endpoint_sha256={endpoint}{dimensions_mode}"
        )
    else:
        identity = "fallback-zero" if dimension == 1024 else f"fallback-zero|dim={dimension}"

    if chunk["chunking_mode"] != "structure-v1":
        return identity
    env = {} if environ is None else environ
    target = max(_env_int(env, ("EMBEDDER_CHUNK_CHARS",), 512), 1)
    hard = max(_env_int(env, ("EMBEDDER_STRUCTURE_HARD_CHARS",), target), target)
    max_chunks = max(_env_int(env, ("EMBEDDER_STRUCTURE_MAX_CHUNKS",), 64), 1)
    max_source_chars = max(
        _env_int(env, ("EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS",), 2_000_000),
        1,
    )
    return (
        f"{identity}|chunking=structure-v1"
        f"|target_chars={target}|hard_chars={hard}"
        f"|max_chunks={max_chunks}|max_source_chars={max_source_chars}"
        "|budget=characters-fallback"
    )


def canonical_json(value: object) -> str:
    """Serialize bounded configuration metadata deterministically."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError):
        raise ControlPlaneValidationError("control_payload_invalid") from None


def _normalize_config(config: Mapping[str, object]) -> dict[str, object]:
    source = _upgrade_legacy_config_shape(
        _strict_mapping(config, reason="control_config_mapping_required")
    )
    _reject_unknown(source, _TOP_LEVEL_FIELDS)
    if set(source) != _TOP_LEVEL_FIELDS:
        raise ControlPlaneValidationError("control_config_incomplete")
    sections: dict[str, Mapping[str, object]] = {}
    for name, allowed in _SECTION_FIELDS.items():
        section = _strict_mapping(
            source.get(name),
            reason="control_config_section_mapping_required",
        )
        _reject_unknown(section, allowed)
        if set(section) != allowed:
            raise ControlPlaneValidationError("control_config_incomplete")
        sections[name] = section

    embedding = _normalize_embedding(sections["embedding"])
    rerank = _normalize_rerank(sections["rerank"])
    chunk = _normalize_chunk_inference(sections["chunk_inference"])
    gateway = _normalize_gateway(sections["gateway"])
    node_routing = _normalize_node_routing(sections["node_routing"])
    normalized = {
        "embedding": embedding,
        "rerank": rerank,
        "chunk_inference": chunk,
        "gateway": gateway,
        "node_routing": node_routing,
    }
    _validate_cross_section(normalized)
    return normalized


def _upgrade_legacy_config_shape(config: dict[str, object]) -> dict[str, object]:
    """Fill complete persisted shapes that predate later managed fields."""

    upgraded = deepcopy(config)
    changed = False

    if "node_routing" not in upgraded and set(upgraded) == (_TOP_LEVEL_FIELDS - {"node_routing"}):
        upgraded["node_routing"] = default_safe_config()["node_routing"]
        changed = True
    node_routing = upgraded.get("node_routing")
    if isinstance(node_routing, dict):
        routing_defaults = default_safe_config()["node_routing"]
        if isinstance(routing_defaults, dict):
            for name, value in routing_defaults.items():
                if name not in node_routing:
                    node_routing[name] = value
                    changed = True

    embedding = upgraded.get("embedding")
    legacy_embedding_fields = _SECTION_FIELDS["embedding"] - {
        "cost_per_million_tokens",
        "cost_currency",
        "pricing_revision",
    }
    if isinstance(embedding, dict) and set(embedding) == legacy_embedding_fields:
        embedding.update(
            {
                "cost_per_million_tokens": None,
                "cost_currency": "",
                "pricing_revision": "",
            }
        )
        changed = True

    chunk = upgraded.get("chunk_inference")
    sampling_defaults = {
        "temperature": 0.0,
        "top_p": 1.0,
        "json_mode": True,
    }
    fusion_defaults = {
        "fusion_mode": "off",
        "fusion_batch_size": 20,
        "fusion_max_wait_seconds": 2.0,
        "fusion_max_queue_size": 1_000,
        "fusion_workers": 2,
        "fusion_lease_seconds": 120,
        "fusion_retry_delay_seconds": 5,
        "fusion_poll_seconds": 0.25,
    }
    all_chunk_fields = _SECTION_FIELDS["chunk_inference"]
    sampling_fields = set(sampling_defaults)
    durable_fusion_fields = {
        "fusion_lease_seconds",
        "fusion_retry_delay_seconds",
        "fusion_poll_seconds",
    }
    original_fusion_fields = set(fusion_defaults) - durable_fusion_fields
    optional_legacy_groups = (
        sampling_fields,
        original_fusion_fields,
        durable_fusion_fields,
    )
    known_legacy_shapes = {
        frozenset(all_chunk_fields - missing)
        for mask in range(1, 1 << len(optional_legacy_groups))
        for missing in [
            set().union(
                *(
                    group
                    for index, group in enumerate(optional_legacy_groups)
                    if mask & (1 << index)
                )
            )
        ]
    }
    if isinstance(chunk, dict):
        fields = set(chunk)
        if fields in known_legacy_shapes:
            for name, value in {**sampling_defaults, **fusion_defaults}.items():
                chunk.setdefault(name, value)
            changed = True
    return upgraded if changed else config


def _normalize_embedding(section: Mapping[str, object]) -> dict[str, object]:
    enabled = _boolean(section["enabled"])
    provider = _choice(section["provider"], {"openai-compatible"})
    base_url = _optional_base_url(section["base_url"])
    path = _endpoint_path(section["path"])
    model = _optional_model(section["model"])
    revision = _optional_model(section["model_revision"])
    cost_per_million_tokens = _optional_number(
        section["cost_per_million_tokens"],
        0.0,
        1_000_000_000.0,
    )
    cost_currency = _text(section["cost_currency"], maximum_bytes=3, allow_empty=True).upper()
    pricing_revision = _text(
        section["pricing_revision"],
        maximum_bytes=256,
        allow_empty=True,
    )
    if cost_currency and cost_currency not in {"USD", "CNY"}:
        raise ControlPlaneValidationError("control_embedding_cost_currency_invalid")
    cost_fields_configured = (
        cost_per_million_tokens is not None,
        bool(cost_currency),
        bool(pricing_revision),
    )
    if any(cost_fields_configured) and not all(cost_fields_configured):
        raise ControlPlaneValidationError("control_embedding_cost_policy_incomplete")

    result = {
        "enabled": enabled,
        "provider": provider,
        "base_url": base_url,
        "path": path,
        "model": model,
        "model_revision": revision,
        "dimension": _integer(section["dimension"], 1, 16_384),
        "send_dimensions": _boolean(section["send_dimensions"]),
        "batch_size": _integer(section["batch_size"], 1, 256),
        "max_input_bytes": _integer(section["max_input_bytes"], 1, 1024 * 1024),
        "max_total_input_bytes": _integer(
            section["max_total_input_bytes"],
            1,
            8 * 1024 * 1024,
        ),
        "max_request_bytes": _integer(
            section["max_request_bytes"],
            1024,
            16 * 1024 * 1024,
        ),
        "max_response_bytes": _integer(
            section["max_response_bytes"],
            1024,
            64 * 1024 * 1024,
        ),
        "timeout_seconds": _number(section["timeout_seconds"], 0.05, 600.0),
        "total_timeout_seconds": _number(
            section["total_timeout_seconds"],
            0.05,
            600.0,
        ),
        "max_retries": _integer(section["max_retries"], 0, 32),
        "cache_size": _integer(section["cache_size"], 0, 100_000),
        "cache_ttl_seconds": _integer(section["cache_ttl_seconds"], 0, 86_400),
        "cost_per_million_tokens": cost_per_million_tokens,
        "cost_currency": cost_currency,
        "pricing_revision": pricing_revision,
    }
    if result["max_total_input_bytes"] < result["max_input_bytes"]:
        raise ControlPlaneValidationError("control_embedding_limits_invalid")
    if result["max_request_bytes"] < result["max_input_bytes"]:
        raise ControlPlaneValidationError("control_embedding_limits_invalid")
    if result["total_timeout_seconds"] < result["timeout_seconds"]:
        raise ControlPlaneValidationError("control_embedding_timeout_invalid")
    if enabled and (not base_url or not model or not revision):
        raise ControlPlaneValidationError("control_embedding_config_incomplete")
    return result


def _normalize_rerank(section: Mapping[str, object]) -> dict[str, object]:
    enabled = _boolean(section["enabled"])
    raw_providers = section["providers"]
    if not isinstance(raw_providers, list) or not raw_providers:
        raise ControlPlaneValidationError("control_rerank_providers_invalid")
    providers = [_canonical_rerank_provider(value) for value in raw_providers]
    if providers[0] != "cloud" or any(value not in {"cloud", "original"} for value in providers):
        raise ControlPlaneValidationError("control_rerank_providers_invalid")
    if len(set(providers)) != len(providers) or providers.count("cloud") != 1:
        raise ControlPlaneValidationError("control_rerank_providers_invalid")
    base_url = _optional_base_url(section["base_url"])
    model = _optional_model(section["model"])
    revision = _optional_model(section["model_revision"])
    result = {
        "enabled": enabled,
        "providers": providers,
        "base_url": base_url,
        "path": _endpoint_path(section["path"]),
        "model": model,
        "model_revision": revision,
        "timeout_seconds": _number(section["timeout_seconds"], 0.05, 600.0),
        "total_timeout_seconds": _number(
            section["total_timeout_seconds"],
            0.05,
            600.0,
        ),
        "max_retries": _integer(section["max_retries"], 0, 32),
        "max_candidates": _integer(section["max_candidates"], 1, 100),
        "max_document_chars": _integer(section["max_document_chars"], 1, 16_000),
        "max_query_chars": _integer(section["max_query_chars"], 1, 16_000),
    }
    if result["total_timeout_seconds"] < result["timeout_seconds"]:
        raise ControlPlaneValidationError("control_rerank_timeout_invalid")
    if enabled and (not base_url or not model or not revision):
        raise ControlPlaneValidationError("control_rerank_config_incomplete")
    return result


def _normalize_chunk_inference(section: Mapping[str, object]) -> dict[str, object]:
    chunking = _choice(section["chunking_mode"], {"off", "shadow", "structure-v1"})
    enrichment = _choice(section["enrichment_mode"], {"off", "shadow", "on"})
    base_url = _optional_base_url(section["base_url"])
    model = _optional_model(section["model"])
    revision = _optional_model(section["model_revision"])
    result = {
        "chunking_mode": chunking,
        "enrichment_mode": enrichment,
        "provider": _choice(section["provider"], {"openai-compatible"}),
        "base_url": base_url,
        "path": _endpoint_path(section["path"]),
        "model": model,
        "model_revision": revision,
        "timeout_seconds": _number(section["timeout_seconds"], 0.1, 600.0),
        "temperature": _number(section["temperature"], 0.0, 2.0),
        "top_p": _number(section["top_p"], 0.0, 1.0),
        "json_mode": _boolean(section["json_mode"]),
        # Structured JSON token requests are provider-adaptive.  Keep the
        # prompt/payload/output byte and timeout controls below, but do not
        # impose the former arbitrary 8192-token ceiling here.
        "num_predict": _unbounded_integer(section["num_predict"], 128),
        "max_output_chars": _integer(section["max_output_chars"], 512, 64 * 1024),
        "queue_size": _integer(section["queue_size"], 1, 10_000),
        "worker_idle_timeout_seconds": _number(
            section["worker_idle_timeout_seconds"],
            0.1,
            3600.0,
        ),
        "fusion_mode": _choice(section["fusion_mode"], {"off", "shadow", "on"}),
        "fusion_batch_size": _integer(section["fusion_batch_size"], 2, 1_000),
        "fusion_max_wait_seconds": _number(
            section["fusion_max_wait_seconds"],
            0.01,
            3_600.0,
        ),
        "fusion_max_queue_size": _integer(
            section["fusion_max_queue_size"],
            2,
            100_000,
        ),
        "fusion_workers": _integer(section["fusion_workers"], 1, 32),
        "fusion_lease_seconds": _integer(section["fusion_lease_seconds"], 5, 900),
        "fusion_retry_delay_seconds": _integer(
            section["fusion_retry_delay_seconds"],
            0,
            86_400,
        ),
        "fusion_poll_seconds": _number(section["fusion_poll_seconds"], 0.05, 10.0),
    }
    if enrichment != "off" and chunking != "structure-v1":
        raise ControlPlaneValidationError("control_chunk_inference_requires_structure_v1")
    if enrichment != "off" and (not base_url or not model or not revision):
        raise ControlPlaneValidationError("control_chunk_inference_config_incomplete")
    if enrichment != "off" and not result["json_mode"]:
        raise ControlPlaneValidationError("control_chunk_inference_requires_json_mode")
    if result["temperature"] != 1.0 and result["top_p"] != 1.0:
        raise ControlPlaneValidationError("control_chunk_inference_sampling_invalid")
    if result["fusion_mode"] != "off" and enrichment == "off":
        raise ControlPlaneValidationError("control_fusion_requires_chunk_inference")
    if result["fusion_mode"] != "off" and chunking != "structure-v1":
        raise ControlPlaneValidationError("control_fusion_requires_structure_v1")
    return result


def _normalize_gateway(section: Mapping[str, object]) -> dict[str, object]:
    enabled = _boolean(section["enabled"])
    project = _text(section["project_id"], maximum_bytes=136, allow_empty=True)
    if project and not project.startswith("project:"):
        project = f"project:{project}"
    if project and not _PROJECT_RE.fullmatch(project):
        raise ControlPlaneValidationError("control_gateway_project_invalid")
    raw_hosts = section["provider_host_allowlist"]
    if not isinstance(raw_hosts, list) or len(raw_hosts) > 32:
        raise ControlPlaneValidationError("control_gateway_host_allowlist_invalid")
    hosts = [_provider_host(value) for value in raw_hosts]
    if len(set(hosts)) != len(hosts):
        raise ControlPlaneValidationError("control_gateway_host_allowlist_invalid")
    result = {
        "enabled": enabled,
        "project_id": project,
        "ttl_seconds": _integer(section["ttl_seconds"], 30, 86_400),
        "lease_seconds": _integer(section["lease_seconds"], 5, 3_600),
        "max_concurrency": _integer(section["max_concurrency"], 1, 32),
        "max_active_jobs": _integer(section["max_active_jobs"], 1, 100_000),
        "retention_seconds": _integer(
            section["retention_seconds"],
            3_600,
            30 * 86_400,
        ),
        "max_retained_rows": _optional_integer(
            section["max_retained_rows"],
            1,
            1_000_000,
        ),
        "max_retained_json_bytes": _integer(
            section["max_retained_json_bytes"],
            1024 * 1024,
            64 * 1024 * 1024 * 1024,
        ),
        "provider_host_allowlist": hosts,
    }
    if result["lease_seconds"] >= result["ttl_seconds"]:
        raise ControlPlaneValidationError("control_gateway_lease_invalid")
    if enabled and not project:
        raise ControlPlaneValidationError("control_gateway_project_invalid")
    return result


def _normalize_node_routing(section: Mapping[str, object]) -> dict[str, object]:
    """Normalize non-secret server-owned routing and accelerator policy.

    This section deliberately carries only opaque node IDs and identity
    digests. Transport addresses and credentials remain local runtime secrets,
    never revision material or Dashboard configuration.
    """

    enabled = _boolean(section["enabled"])
    policies = {
        "remote-node-first",
        "fastest-estimated",
        "ollama-first",
        "pinned-node",
    }
    raw_nodes = section["allowed_node_ids"]
    if not isinstance(raw_nodes, list) or len(raw_nodes) > 64:
        raise ControlPlaneValidationError("control_node_routing_allowed_nodes_invalid")
    allowed_node_ids = [_node_id(value) for value in raw_nodes]
    if len(set(allowed_node_ids)) != len(allowed_node_ids):
        raise ControlPlaneValidationError("control_node_routing_allowed_nodes_invalid")
    result = {
        "enabled": enabled,
        "inference_mode": _choice(section["inference_mode"], {"local", "cloud", "hybrid"}),
        "embedding_policy": _choice(section["embedding_policy"], policies),
        "rerank_policy": _choice(section["rerank_policy"], policies),
        "structured_json_policy": _choice(section["structured_json_policy"], policies),
        "embedding_required_identity": _optional_sha256(
            section["embedding_required_identity"],
            "control_node_routing_embedding_identity_invalid",
        ),
        "rerank_required_identity": _optional_sha256(
            section["rerank_required_identity"],
            "control_node_routing_rerank_identity_invalid",
        ),
        "structured_json_required_identity": _optional_sha256(
            section["structured_json_required_identity"],
            "control_node_routing_structured_json_identity_invalid",
        ),
        "embedding_pinned_node_id": _optional_node_id(section["embedding_pinned_node_id"]),
        "rerank_pinned_node_id": _optional_node_id(section["rerank_pinned_node_id"]),
        "structured_json_pinned_node_id": _optional_node_id(
            section["structured_json_pinned_node_id"]
        ),
        "allowed_node_ids": allowed_node_ids,
        "project_overrides": _normalize_project_overrides(section.get("project_overrides", {})),
        "accelerator_max_enabled": _boolean(section["accelerator_max_enabled"]),
        "accelerator_max_concurrency": _integer(section["accelerator_max_concurrency"], 1, 64),
        "accelerator_max_queue_depth": _integer(section["accelerator_max_queue_depth"], 1, 100_000),
        "accelerator_max_daily_tasks": _integer(
            section["accelerator_max_daily_tasks"], 1, 1_000_000
        ),
        "accelerator_min_free_memory_mib": _integer(
            section["accelerator_min_free_memory_mib"], 0, 1_000_000
        ),
    }
    if not enabled:
        if result["accelerator_max_enabled"]:
            raise ControlPlaneValidationError("control_accelerator_requires_node_routing")
        return result
    if not allowed_node_ids:
        raise ControlPlaneValidationError("control_node_routing_allowed_nodes_required")
    for operation, identity_key, pin_key in (
        ("embedding", "embedding_required_identity", "embedding_pinned_node_id"),
        ("rerank", "rerank_required_identity", "rerank_pinned_node_id"),
        ("structured_json", "structured_json_required_identity", "structured_json_pinned_node_id"),
    ):
        policy = result[f"{operation}_policy"]
        pin = result[pin_key]
        if policy == "pinned-node" and not pin:
            raise ControlPlaneValidationError("control_node_routing_pinned_node_required")
        if pin and pin not in allowed_node_ids:
            raise ControlPlaneValidationError("control_node_routing_pinned_node_forbidden")
        # Existing revisions predate structured-json routing.  Keep that
        # operation disabled until its identity is explicitly declared; the
        # foreground runtime fails closed rather than inventing a provider.
        if identity_key is not None and not result[identity_key] and operation != "structured_json":
            raise ControlPlaneValidationError("control_node_routing_identity_required")
    return result


def _normalize_project_overrides(value: object) -> dict[str, dict[str, object]]:
    """Normalize project-scoped hot-routing overlays without secrets."""

    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 256:
        raise ControlPlaneValidationError("control_node_routing_project_overrides_invalid")
    normalized: dict[str, dict[str, object]] = {}
    allowed = {"inference_mode", "embedding_policy", "rerank_policy", "structured_json_policy"}
    for project_id, raw in value.items():
        if not isinstance(project_id, str) or not _PROJECT_RE.fullmatch(project_id):
            raise ControlPlaneValidationError("control_node_routing_project_id_invalid")
        if not isinstance(raw, Mapping) or not set(raw).issubset(allowed):
            raise ControlPlaneValidationError("control_node_routing_project_overrides_invalid")
        item: dict[str, object] = {}
        if "inference_mode" in raw:
            item["inference_mode"] = _choice(raw["inference_mode"], {"local", "cloud", "hybrid"})
        policies = {"remote-node-first", "fastest-estimated", "ollama-first", "pinned-node"}
        for name in ("embedding_policy", "rerank_policy", "structured_json_policy"):
            if name in raw:
                item[name] = _choice(raw[name], policies)
        normalized[project_id] = item
    return normalized


def _validate_cross_section(config: Mapping[str, object]) -> None:
    embedding = config["embedding"]
    rerank = config["rerank"]
    chunk = config["chunk_inference"]
    gateway = config["gateway"]
    node_routing = config["node_routing"]
    assert isinstance(embedding, dict)
    assert isinstance(rerank, dict)
    assert isinstance(chunk, dict)
    assert isinstance(gateway, dict)
    assert isinstance(node_routing, dict)
    if chunk["enrichment_mode"] == "on" and not embedding["enabled"]:
        raise ControlPlaneValidationError("control_chunk_inference_requires_embedding")
    required_hosts = {
        _url_host(section["base_url"])
        for section in (embedding, rerank, chunk)
        if (
            (section is embedding and embedding["enabled"])
            or (section is rerank and rerank["enabled"])
            or (section is chunk and chunk["enrichment_mode"] != "off")
        )
    }
    allowed_hosts = set(gateway["provider_host_allowlist"])
    if not required_hosts.issubset(allowed_hosts):
        raise ControlPlaneValidationError("control_gateway_provider_host_missing")


def _validate_required_secrets(
    config: Mapping[str, object],
    values: Mapping[str, str],
) -> None:
    for name, value in values.items():
        if value and not _SAFE_SECRET_RE.fullmatch(value):
            raise ControlPlaneValidationError("control_secret_value_invalid")
        if name == "gateway_token" and value and not _GATEWAY_TOKEN_RE.fullmatch(value):
            raise ControlPlaneValidationError("control_gateway_token_invalid")
    embedding = config["embedding"]
    rerank = config["rerank"]
    chunk = config["chunk_inference"]
    gateway = config["gateway"]
    node_routing = config["node_routing"]
    assert isinstance(embedding, dict)
    assert isinstance(rerank, dict)
    assert isinstance(chunk, dict)
    assert isinstance(gateway, dict)
    assert isinstance(node_routing, dict)
    governed_compute_route = node_routing["enabled"] is True
    requirements = (
        (embedding["enabled"] and not governed_compute_route, "embedding_api_key"),
        (rerank["enabled"] and not governed_compute_route, "rerank_api_key"),
        (
            chunk["enrichment_mode"] != "off" and not governed_compute_route,
            "chunk_inference_api_key",
        ),
        (gateway["enabled"] and not governed_compute_route, "gateway_token"),
        (
            node_routing["enabled"] and node_routing["inference_mode"] in {"cloud", "hybrid"},
            "compute_node_cloud_api_key",
        ),
    )
    if any(required and not values.get(name) for required, name in requirements):
        raise ControlPlaneValidationError("control_required_secret_missing")


def _render_environment(
    config: Mapping[str, object],
    values: Mapping[str, str],
) -> dict[str, str]:
    embedding = config["embedding"]
    rerank = config["rerank"]
    chunk = config["chunk_inference"]
    gateway = config["gateway"]
    node_routing = config["node_routing"]
    assert isinstance(embedding, dict)
    assert isinstance(rerank, dict)
    assert isinstance(chunk, dict)
    assert isinstance(gateway, dict)
    assert isinstance(node_routing, dict)
    dimension = int(embedding["dimension"])
    model = str(embedding["model"])
    env = {
        "EMBEDDER_PROVIDER": ("openai-compatible" if embedding["enabled"] else "fallback"),
        "EMBEDDER_BASE_URL": str(embedding["base_url"]),
        "EMBEDDER_PATH": str(embedding["path"]),
        "EMBEDDER_MODEL": model,
        "EMBEDDER_MODEL_REVISION": str(embedding["model_revision"]),
        "EMBEDDER_DIMENSION": str(dimension),
        "PP_EMBEDDING_DIM": str(dimension),
        "EMBEDDER_SEND_DIMENSIONS": _env_boolean(embedding["send_dimensions"]),
        "EMBEDDER_BATCH_SIZE": str(embedding["batch_size"]),
        "EMBEDDER_MAX_INPUT_BYTES": str(embedding["max_input_bytes"]),
        "EMBEDDER_MAX_TOTAL_INPUT_BYTES": str(embedding["max_total_input_bytes"]),
        "EMBEDDER_MAX_REQUEST_BYTES": str(embedding["max_request_bytes"]),
        "EMBEDDER_MAX_RESPONSE_BYTES": str(embedding["max_response_bytes"]),
        "EMBEDDER_TIMEOUT": _env_number(embedding["timeout_seconds"]),
        "EMBEDDER_TOTAL_TIMEOUT": _env_number(embedding["total_timeout_seconds"]),
        "EMBEDDER_MAX_RETRIES": str(embedding["max_retries"]),
        "EMBEDDER_CACHE_SIZE": str(embedding["cache_size"]),
        "EMBEDDER_CACHE_TTL": str(embedding["cache_ttl_seconds"]),
        "EMBEDDER_COST_PER_MILLION_TOKENS": (
            ""
            if embedding["cost_per_million_tokens"] is None
            else _env_number(embedding["cost_per_million_tokens"])
        ),
        "EMBEDDER_COST_CURRENCY": str(embedding["cost_currency"]),
        "EMBEDDER_PRICING_REVISION": str(embedding["pricing_revision"]),
        "PP_RERANK_DISABLED": "0" if rerank["enabled"] else "1",
        "PP_RERANK_PROVIDERS": ",".join(rerank["providers"]),
        "PP_RERANK_BASE_URL": str(rerank["base_url"]),
        "PP_RERANK_PATH": str(rerank["path"]),
        "PP_RERANK_CLOUD_MODEL": str(rerank["model"]),
        "PP_RERANK_CLOUD_MODEL_REVISION": str(rerank["model_revision"]),
        "PP_RERANK_TIMEOUT_SEC": _env_number(rerank["timeout_seconds"]),
        "PP_RERANK_TOTAL_TIMEOUT_SEC": _env_number(rerank["total_timeout_seconds"]),
        "PP_RERANK_MAX_RETRIES": str(rerank["max_retries"]),
        "PP_RERANK_MAX_CANDIDATES": str(rerank["max_candidates"]),
        "PP_RERANK_MAX_DOCUMENT_CHARS": str(rerank["max_document_chars"]),
        "PP_RERANK_MAX_QUERY_CHARS": str(rerank["max_query_chars"]),
        "PP_MEMORY_CHUNKING": str(chunk["chunking_mode"]),
        "PP_MEMORY_CHUNK_ENRICHMENT": str(chunk["enrichment_mode"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_PROVIDER": str(chunk["provider"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_BASE_URL": str(chunk["base_url"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_PATH": str(chunk["path"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_MODEL": str(chunk["model"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_MODEL_REVISION": str(chunk["model_revision"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_TIMEOUT": _env_number(chunk["timeout_seconds"]),
        "PP_INFERENCE_TIMEOUT_SEC": _env_number(chunk["timeout_seconds"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_TEMPERATURE": _env_number(chunk["temperature"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_TOP_P": _env_number(chunk["top_p"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_JSON_MODE": _env_boolean(chunk["json_mode"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_NUM_PREDICT": str(chunk["num_predict"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_MAX_OUTPUT_CHARS": str(chunk["max_output_chars"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_QUEUE_SIZE": str(chunk["queue_size"]),
        "PP_MEMORY_CHUNK_ENRICHMENT_WORKER_IDLE_TIMEOUT": _env_number(
            chunk["worker_idle_timeout_seconds"]
        ),
        "PP_STRUCTURED_MEMORY_FUSION": str(chunk["fusion_mode"]),
        "PP_STRUCTURED_MEMORY_FUSION_BATCH_SIZE": str(chunk["fusion_batch_size"]),
        "PP_STRUCTURED_MEMORY_FUSION_MAX_WAIT_SECONDS": _env_number(
            chunk["fusion_max_wait_seconds"]
        ),
        "PP_STRUCTURED_MEMORY_FUSION_MAX_QUEUE": str(chunk["fusion_max_queue_size"]),
        "PP_STRUCTURED_MEMORY_FUSION_WORKERS": str(chunk["fusion_workers"]),
        "PP_STRUCTURED_MEMORY_FUSION_LEASE_SECONDS": str(chunk["fusion_lease_seconds"]),
        "PP_STRUCTURED_MEMORY_FUSION_RETRY_DELAY_SECONDS": str(chunk["fusion_retry_delay_seconds"]),
        "PP_STRUCTURED_MEMORY_FUSION_POLL_SECONDS": _env_number(chunk["fusion_poll_seconds"]),
        "PP_INFERENCE_GATEWAY": "1" if gateway["enabled"] else "0",
        "PP_INFERENCE_GATEWAY_TTL_SEC": str(gateway["ttl_seconds"]),
        "PP_INFERENCE_GATEWAY_LEASE_SEC": str(gateway["lease_seconds"]),
        "PP_INFERENCE_GATEWAY_MAX_CONCURRENCY": str(gateway["max_concurrency"]),
        "PP_INFERENCE_GATEWAY_MAX_ACTIVE_JOBS": str(gateway["max_active_jobs"]),
        "PP_INFERENCE_GATEWAY_RETENTION_SEC": str(gateway["retention_seconds"]),
        "PP_INFERENCE_GATEWAY_MAX_RETAINED_ROWS": (
            "" if gateway["max_retained_rows"] is None else str(gateway["max_retained_rows"])
        ),
        "PP_INFERENCE_GATEWAY_MAX_RETAINED_JSON_BYTES": str(gateway["max_retained_json_bytes"]),
        # Deployment supervisor consumes this only when materializing the
        # compute-node package; pp-server-backend never constructs a provider
        # from it.
        "PP_LOCAL_NODE_PROVIDER_MODE": str(node_routing["inference_mode"]),
    }
    # Compute credentials are intentionally absent from the server-managed
    # EnvironmentFile.  The server may retain only a write-only presence flag;
    # a compute supervisor receives the separate private projection below.
    governed_compute_route = node_routing.get("enabled") is True
    if governed_compute_route:
        # ``managed.env`` belongs to pp-server-backend.  Once governed node
        # routing is enabled it may retain schema limits and canonical routing
        # metadata, but it must not materialize a provider adapter, endpoint,
        # model runtime or legacy inference gateway.  The complete provider
        # projection is emitted separately by ``_render_compute_environment``.
        env.update(
            {
                "EMBEDDER_PROVIDER": "fallback",
                "EMBEDDER_BASE_URL": "",
                "EMBEDDER_PATH": "",
                "PP_RERANK_DISABLED": "1",
                "PP_RERANK_PROVIDERS": "original",
                "PP_RERANK_BASE_URL": "",
                "PP_RERANK_PATH": "",
                "PP_MEMORY_CHUNK_ENRICHMENT": "off",
                "PP_MEMORY_CHUNK_ENRICHMENT_PROVIDER": "disabled",
                "PP_MEMORY_CHUNK_ENRICHMENT_BASE_URL": "",
                "PP_MEMORY_CHUNK_ENRICHMENT_PATH": "",
                "PP_MEMORY_CHUNK_ENRICHMENT_MODEL": "",
                "PP_MEMORY_CHUNK_ENRICHMENT_MODEL_REVISION": "",
                "PP_INFERENCE_GATEWAY": "0",
            }
        )
        env.pop("PP_LOCAL_NODE_PROVIDER_MODE", None)
    for name in _REMOTE_SECRET_NAMES - {"compute_node_cloud_api_key"}:
        # Once node routing is enabled, provider credentials belong only to
        # the compute projection.  Keep the legacy server projection for
        # installations that have not opted into the governed route.
        if governed_compute_route:
            continue
        env[_SECRET_ENV_NAMES[name]] = values.get(name, "")
    return dict(sorted(env.items()))


def _render_compute_environment(
    config: Mapping[str, object],
    values: Mapping[str, str],
) -> dict[str, str]:
    """Render the compute-only private projection.

    This projection is never returned as public config and is never merged
    into ``managed.env``.  A deployment supervisor may materialize it only on
    ``pp-compute-node`` after an identity check.
    """

    node_routing = config.get("node_routing")
    if not isinstance(node_routing, Mapping):
        return {}
    if node_routing.get("enabled") is not True:
        return {}
    mode = str(node_routing.get("inference_mode", "local"))
    if mode not in {"local", "cloud", "hybrid"}:
        return {}
    value = str(values.get("compute_node_cloud_api_key") or "")
    if mode in {"cloud", "hybrid"} and not value:
        return {}

    embedding = config.get("embedding")
    rerank = config.get("rerank")
    chunk = config.get("chunk_inference")
    if (
        not isinstance(embedding, Mapping)
        or not isinstance(rerank, Mapping)
        or not isinstance(chunk, Mapping)
    ):
        return {}

    environment = {
        "PP_ENDPOINT_ROLE": "pp-compute-node",
        "PP_LOCAL_NODE_PROVIDER_MODE": mode,
    }
    if value:
        environment["PP_LOCAL_NODE_CLOUD_API_KEY"] = value
    if embedding.get("enabled") is True:
        environment.update(
            {
                "PP_LOCAL_NODE_EMBEDDING_MODEL": str(embedding.get("model") or ""),
                "PP_LOCAL_NODE_EMBEDDING_REVISION": str(embedding.get("model_revision") or ""),
                "PP_LOCAL_NODE_EMBEDDING_DIMENSION": str(embedding.get("dimension") or ""),
                "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION": "l2",
            }
        )
        if mode in {"cloud", "hybrid"}:
            environment.update(
                {
                    "PP_LOCAL_NODE_EMBEDDING_BACKEND": "openai-compatible",
                    "PP_LOCAL_NODE_EMBEDDING_CLOUD_BASE_URL": str(embedding.get("base_url") or ""),
                    "PP_LOCAL_NODE_EMBEDDING_CLOUD_PATH": str(
                        embedding.get("path") or "/embeddings"
                    ),
                }
            )
    if rerank.get("enabled") is True:
        environment.update(
            {
                "PP_LOCAL_NODE_RERANK_MODEL": str(rerank.get("model") or ""),
                "PP_LOCAL_NODE_RERANK_REVISION": str(rerank.get("model_revision") or ""),
            }
        )
        if mode in {"cloud", "hybrid"}:
            environment.update(
                {
                    "PP_LOCAL_NODE_RERANK_BACKEND": "openai-compatible",
                    "PP_LOCAL_NODE_RERANK_CLOUD_BASE_URL": str(rerank.get("base_url") or ""),
                    "PP_LOCAL_NODE_RERANK_CLOUD_PATH": str(rerank.get("path") or "/rerank"),
                }
            )
    if chunk.get("enrichment_mode") != "off":
        environment.update(
            {
                "PP_LOCAL_NODE_STRUCTURED_JSON_MODEL": str(chunk.get("model") or ""),
                "PP_LOCAL_NODE_STRUCTURED_JSON_REVISION": str(chunk.get("model_revision") or ""),
            }
        )
        if mode in {"cloud", "hybrid"}:
            environment.update(
                {
                    "PP_LOCAL_NODE_STRUCTURED_JSON_BACKEND": "openai-compatible",
                    "PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_BASE_URL": str(
                        chunk.get("base_url") or ""
                    ),
                    "PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_PATH": str(
                        chunk.get("path") or "/chat/completions"
                    ),
                }
            )
    return dict(sorted(environment.items()))


def _normalize_secret_operations(
    secret_ops: Mapping[str, object],
) -> dict[str, dict[str, str]]:
    operations = _strict_mapping(secret_ops, reason="control_secret_operations_mapping_required")
    # The gateway bearer token is bootstrap-owned authentication material.
    # It is retained in private revisions, but is never remotely mutable.
    _reject_unknown(operations, _REMOTE_SECRET_NAMES, secret=True)
    normalized: dict[str, dict[str, str]] = {}
    for name, raw_operation in operations.items():
        operation = _strict_mapping(
            raw_operation,
            reason="control_secret_operation_mapping_required",
        )
        op = operation.get("op")
        allowed = {"op", "value"} if op == "set" else {"op"}
        if set(operation) != allowed or op not in {"set", "clear"}:
            raise ControlPlaneValidationError("control_secret_operation_invalid")
        if op == "set":
            value = operation.get("value")
            if not isinstance(value, str) or not _SAFE_SECRET_RE.fullmatch(value):
                raise ControlPlaneValidationError("control_secret_value_invalid")
            normalized[name] = {"op": "set", "value": value}
        else:
            normalized[name] = {"op": "clear"}
    return normalized


def _strict_mapping(value: object, *, reason: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ControlPlaneValidationError(reason)
    return dict(value)


def _reject_unknown(
    mapping: Mapping[str, object],
    allowed: frozenset[str],
    *,
    secret: bool = False,
) -> None:
    if not set(mapping).issubset(allowed):
        reason = (
            "control_secret_field_not_allowed" if secret else "control_config_field_not_allowed"
        )
        raise ControlPlaneValidationError(reason)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ControlPlaneValidationError("control_config_value_invalid")
    return value


def _integer(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ControlPlaneValidationError("control_config_value_invalid")
    return value


def _unbounded_integer(value: object, minimum: int) -> int:
    """Validate a positive integer without an arbitrary upper ceiling."""

    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ControlPlaneValidationError("control_config_value_invalid")
    return value


def _optional_integer(value: object, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    return _integer(value, minimum, maximum)


def _optional_number(value: object, minimum: float, maximum: float) -> float | None:
    if value is None:
        return None
    return _number(value, minimum, maximum)


def _number(value: object, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise ControlPlaneValidationError("control_config_value_invalid")
    return float(value)


def _choice(value: object, allowed: set[str]) -> str:
    if not isinstance(value, str):
        raise ControlPlaneValidationError("control_config_value_invalid")
    normalized = value.strip().casefold()
    if value != value.strip() or normalized not in allowed:
        raise ControlPlaneValidationError("control_config_value_invalid")
    return normalized


def _text(value: object, *, maximum_bytes: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or value != value.strip() or "\x00" in value:
        raise ControlPlaneValidationError("control_config_value_invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ControlPlaneValidationError("control_config_value_invalid") from None
    if size > maximum_bytes or (not allow_empty and not value):
        raise ControlPlaneValidationError("control_config_value_invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ControlPlaneValidationError("control_config_value_invalid")
    return value


def _optional_model(value: object) -> str:
    text = _text(value, maximum_bytes=256, allow_empty=True)
    if text and not _MODEL_RE.fullmatch(text):
        raise ControlPlaneValidationError("control_model_invalid")
    return text


def _node_id(value: object) -> str:
    text = _text(value, maximum_bytes=128)
    if not _NODE_ID_RE.fullmatch(text):
        raise ControlPlaneValidationError("control_node_routing_node_id_invalid")
    return text


def _optional_node_id(value: object) -> str:
    text = _text(value, maximum_bytes=128, allow_empty=True)
    return "" if not text else _node_id(text)


def _optional_sha256(value: object, reason: str) -> str:
    text = _text(value, maximum_bytes=71, allow_empty=True)
    if text and not _SHA256_RE.fullmatch(text):
        raise ControlPlaneValidationError(reason)
    return text


def _optional_base_url(value: object) -> str:
    text = _text(value, maximum_bytes=2048, allow_empty=True)
    return "" if not text else _base_url(text)


def _base_url(value: str) -> str:
    if any(character in value for character in ("\\", "?", "#")):
        raise ControlPlaneValidationError("control_provider_base_url_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ControlPlaneValidationError("control_provider_base_url_invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ControlPlaneValidationError("control_provider_base_url_invalid")
    host = _provider_host(parsed.hostname)
    if any(label in _DOCUMENTATION_HOST_LABELS for label in host.split(".")):
        raise ControlPlaneValidationError("control_provider_documentation_url")
    decoded = unquote(parsed.path)
    if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
        raise ControlPlaneValidationError("control_provider_base_url_invalid")
    authority = host if port is None else f"{host}:{port}"
    return f"https://{authority}{parsed.path}".rstrip("/")


def _endpoint_path(value: object) -> str:
    path = _text(value, maximum_bytes=512)
    if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\\?#"):
        raise ControlPlaneValidationError("control_provider_path_invalid")
    decoded = unquote(path)
    if any(part in {".", ".."} for part in decoded.split("/")):
        raise ControlPlaneValidationError("control_provider_path_invalid")
    return path


def _provider_host(value: object) -> str:
    text = _text(value, maximum_bytes=253).rstrip(".").casefold()
    try:
        address = ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        try:
            host = text.encode("idna").decode("ascii")
        except UnicodeError:
            raise ControlPlaneValidationError("control_gateway_host_allowlist_invalid") from None
        if not _HOST_RE.fullmatch(host):
            raise ControlPlaneValidationError("control_gateway_host_allowlist_invalid") from None
        return host
    if not address.is_global:
        raise ControlPlaneValidationError("control_gateway_host_allowlist_invalid")
    return str(address)


def _url_host(value: object) -> str:
    parsed = urlsplit(str(value))
    if not parsed.hostname:
        raise ControlPlaneValidationError("control_provider_base_url_invalid")
    return _provider_host(parsed.hostname)


def _canonical_rerank_provider(value: object) -> str:
    if not isinstance(value, str):
        raise ControlPlaneValidationError("control_rerank_providers_invalid")
    normalized = value.strip().casefold()
    if value != value.strip():
        raise ControlPlaneValidationError("control_rerank_providers_invalid")
    return "original" if normalized == "cosine" else normalized


def _env_text(
    environ: Mapping[str, object],
    name: str,
    default: str,
) -> str:
    value = environ.get(name)
    return default if value is None else str(value).strip()


def _env_int(
    environ: Mapping[str, object],
    names: tuple[str, ...],
    default: int,
) -> int:
    for name in names:
        value = environ.get(name)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
    return default


def _env_float(
    environ: Mapping[str, object],
    names: tuple[str, ...],
    default: float,
) -> float:
    for name in names:
        value = environ.get(name)
        if value not in (None, ""):
            try:
                parsed = float(value)
                return parsed if math.isfinite(parsed) else default
            except (TypeError, ValueError):
                return default
    return default


def _env_optional_float(
    environ: Mapping[str, object],
    names: tuple[str, ...],
) -> float | None:
    for name in names:
        value = environ.get(name)
        if value not in (None, ""):
            try:
                parsed = float(value)
                return parsed if math.isfinite(parsed) else None
            except (TypeError, ValueError):
                return None
    return None


def _env_bool(
    environ: Mapping[str, object],
    name: str,
    default: bool,
) -> bool:
    value = environ.get(name)
    if value is None:
        return default
    return str(value).strip().casefold() not in {"0", "false", "no", "off"}


def _env_boolean(value: object) -> str:
    return "1" if value is True else "0"


def _env_number(value: object) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else format(number, ".15g")


def _canonical_sha256(value: object) -> str:
    encoded = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BOOTSTRAP_ONLY_ENV_NAMES",
    "CONFIG_CONTRACT",
    "ControlPlaneError",
    "ControlPlaneValidationError",
    "PreparedConfiguration",
    "bootstrap_boundary_sha256",
    "canonical_json",
    "default_safe_config",
    "embedding_identity",
    "normalize_safe_config",
    "routing_for_project",
    "prepare_configuration",
    "runtime_embedding_index_identity",
    "safe_config_from_environment",
    "secret_state",
    "secret_values_from_environment",
]
