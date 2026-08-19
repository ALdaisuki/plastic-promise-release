"""Read-only, redacted local evidence for the deployment doctor.

The deployment controller deliberately does not start services, connect to
nodes, or execute platform management commands.  This module lets the doctor
inspect explicitly supplied non-secret configuration and status evidence
without printing endpoints, model paths, identities, tunnel targets, or any
credential material.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


_NODE_PREFIX = "PP_LOCAL_NODE_"
_TUNNEL_PREFIX = "PP_TUNNEL_"
_SENSITIVE_KEY_TOKENS = frozenset(
    {"APIKEY", "AUTHORIZATION", "CREDENTIAL", "PASSWORD", "PRIVATEKEY", "SECRET", "TOKEN"}
)
_PINNED_REVISION = re.compile(r"(?:[0-9a-f]{40}|sha256:[0-9a-f]{64})$")
_SAFE_IDENTIFIER = re.compile(r"^[^\s]{1,256}$")
_UNPINNED_REVISIONS = frozenset({"latest", "main", "master", "stable", "head"})
_RUNTIME_STATUS_SCHEMA = "plastic-promise/local-inference-runtime-status/v1"


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^A-Z0-9]", "", key.upper())
    return any(token in normalized for token in _SENSITIVE_KEY_TOKENS)


def _read_environment_file(path: Path, *, prefixes: tuple[str, ...]) -> tuple[str, dict[str, str]]:
    """Load a narrow .env-style file without retaining unrelated values."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "unreadable", {}

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            return "invalid", {}
        if _is_sensitive_key(key):
            return "sensitive_key_rejected", {}
        if not any(key.startswith(prefix) for prefix in prefixes) or key in values:
            return "invalid", {}
        values[key] = value.strip().strip("\"'")
    return ("configured" if values else "not_configured"), values


def _environment_values(
    environment: Mapping[str, str], *, prefixes: tuple[str, ...]
) -> tuple[str, dict[str, str]]:
    """Select only the documented, non-secret prefix from process environment."""

    values: dict[str, str] = {}
    for key, value in environment.items():
        if not any(key.startswith(prefix) for prefix in prefixes):
            continue
        if _is_sensitive_key(key):
            return "sensitive_key_rejected", {}
        values[key] = value
    return ("configured" if values else "not_configured"), values


def _observe_environment(
    path: Path | None,
    *,
    prefixes: tuple[str, ...],
    environment: Mapping[str, str],
) -> tuple[str, dict[str, str], str]:
    if path is not None:
        status, values = _read_environment_file(path, prefixes=prefixes)
        return status, values, "file"
    status, values = _environment_values(environment, prefixes=prefixes)
    return status, values, "environment"


def _pinned_revision(value: str | None) -> bool:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        return False
    return (
        value.casefold() not in _UNPINNED_REVISIONS and _PINNED_REVISION.search(value) is not None
    )


def _model_cache_status(values: Mapping[str, str], *, source_status: str) -> dict[str, object]:
    if source_status != "configured":
        return {"status": source_status, "evidence": "node_configuration"}
    if values.get("PP_LOCAL_NODE_EMBEDDING_BACKEND", "bge-local").casefold() == "ollama":
        return {
            "status": "identity_proof_unavailable",
            "evidence": "ollama_mutable_tag_not_governed",
        }
    required = (
        "PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE",
        "PP_LOCAL_NODE_RERANK_MODEL_REFERENCE",
    )
    if any(not values.get(key, "").strip() for key in required):
        return {"status": "missing", "evidence": "node_configuration"}
    cache_directory = values.get("PP_LOCAL_NODE_MODEL_CACHE_DIR", "").strip()
    if not cache_directory:
        return {"status": "configured_unverified", "evidence": "node_configuration"}
    cache_path = Path(cache_directory).expanduser()
    try:
        if not cache_path.exists():
            return {"status": "cache_path_missing", "evidence": "node_configuration"}
        if not cache_path.is_dir() or not os.access(cache_path, os.R_OK):
            return {"status": "cache_path_unreadable", "evidence": "node_configuration"}
        references = tuple(values[key].strip() for key in required)
        paths = tuple(
            candidate if candidate.is_absolute() else cache_path / candidate
            for candidate in (Path(reference).expanduser() for reference in references)
        )
        if any(not path.exists() for path in paths):
            return {"status": "model_reference_missing", "evidence": "local_model_references"}
        if any(not os.access(path, os.R_OK) for path in paths):
            return {"status": "model_reference_unreadable", "evidence": "local_model_references"}
    except OSError:
        return {"status": "cache_path_unreadable", "evidence": "node_configuration"}
    return {"status": "available", "evidence": "readable_model_references"}


def _identity_status(
    values: Mapping[str, str], *, source_status: str, expected_node_id: str | None
) -> dict[str, object]:
    if source_status != "configured":
        return {"status": source_status, "evidence": "node_configuration"}
    node_id = values.get("PP_LOCAL_NODE_ID", "inference-node")
    if not _SAFE_IDENTIFIER.fullmatch(node_id):
        return {"status": "invalid", "evidence": "node_configuration"}
    if expected_node_id is not None and node_id != expected_node_id:
        return {"status": "mismatch", "evidence": "declared_node_identity"}
    revisions = (
        values.get("PP_LOCAL_NODE_EMBEDDING_REVISION"),
        values.get("PP_LOCAL_NODE_RERANK_REVISION"),
    )
    if any(value is None or not value.strip() for value in revisions):
        return {"status": "missing", "evidence": "node_configuration"}
    if not all(_pinned_revision(value) for value in revisions):
        return {"status": "invalid", "evidence": "node_configuration"}
    return {"status": "configured", "evidence": "pinned_revisions"}


def _tunnel_status(values: Mapping[str, str], *, source_status: str) -> dict[str, object]:
    if source_status != "configured":
        return {"status": source_status, "evidence": "tunnel_configuration"}
    required = (
        "PP_TUNNEL_TARGET",
        "PP_TUNNEL_IDENTITY_FILE",
        "PP_TUNNEL_SERVER_PORT",
        "PP_LOCAL_NODE_PORT",
    )
    if any(not values.get(key, "").strip() for key in required):
        return {"status": "missing", "evidence": "tunnel_configuration"}
    try:
        server_port = int(values["PP_TUNNEL_SERVER_PORT"])
        node_port = int(values["PP_LOCAL_NODE_PORT"])
    except ValueError:
        return {"status": "invalid", "evidence": "tunnel_configuration"}
    if not (1 <= server_port <= 65535 and 1 <= node_port <= 65535):
        return {"status": "invalid", "evidence": "tunnel_configuration"}
    return {"status": "configured", "evidence": "restricted_tunnel_contract"}


def _runtime_status(path: Path | None) -> dict[str, object]:
    if path is None:
        return {"status": "not_observed", "evidence": "runtime_status_file"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "unreadable", "evidence": "runtime_status_file"}
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "running",
        "node_healthy",
    }:
        return {"status": "invalid", "evidence": "runtime_status_file"}
    if (
        payload.get("schema_version") != _RUNTIME_STATUS_SCHEMA
        or not isinstance(payload.get("running"), bool)
        or not isinstance(payload.get("node_healthy"), bool)
    ):
        return {"status": "invalid", "evidence": "runtime_status_file"}
    if not payload["running"]:
        return {"status": "stopped", "evidence": "runtime_status_file"}
    return {
        "status": "running" if payload["node_healthy"] else "unhealthy",
        "evidence": "runtime_status_file",
    }


def observe_node_evidence(
    *,
    node_config: Path | None,
    tunnel_config: Path | None,
    runtime_status: Path | None,
    expected_node_id: str | None = None,
    expected_capabilities: tuple[str, ...] = (),
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return bounded diagnostic states without serialising configuration values."""

    selected_environment = os.environ if environment is None else environment
    node_source_status, node_values, node_source = _observe_environment(
        node_config,
        prefixes=(_NODE_PREFIX,),
        environment=selected_environment,
    )
    tunnel_source_status, tunnel_values, tunnel_source = _observe_environment(
        tunnel_config,
        prefixes=(_TUNNEL_PREFIX, "PP_LOCAL_NODE_PORT"),
        environment=selected_environment,
    )
    identity = _identity_status(
        node_values,
        source_status=node_source_status,
        expected_node_id=expected_node_id,
    )
    models = _model_cache_status(node_values, source_status=node_source_status)
    tunnel = _tunnel_status(tunnel_values, source_status=tunnel_source_status)
    identity["source"] = node_source
    models["source"] = node_source
    tunnel["source"] = tunnel_source
    declaration: dict[str, object] = {"status": "not_declared", "capabilities": []}
    if expected_node_id is not None:
        accepted = identity["status"] == "configured" and models["status"] == "available"
        declaration = {
            "status": (
                "declaration_evidence_accepted" if accepted else "declaration_evidence_pending"
            ),
            "capabilities": list(expected_capabilities),
        }
    return {
        "identity": identity,
        "models": models,
        "tunnel": tunnel,
        "runtime": _runtime_status(runtime_status),
        "declaration": declaration,
    }


__all__ = ["observe_node_evidence"]
