"""Server-only resolver for private loopback inference-node endpoints.

The resolver deliberately reads endpoint material outside control revisions and
deployment manifests.  A revision can choose an opaque node ID, but only this
server-local file plus the process environment can resolve its loopback tunnel
address and optional authorization value.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from plastic_promise.core.node_governance import NodeGovernanceError
from plastic_promise.core.node_private_transport import (
    PrivateNodeEndpoint,
    valid_private_node_authorization,
)

_IDENTIFIER_RE = re.compile(r"\A[a-z][a-z0-9_.:-]{1,127}\Z")
_ENV_NAME_RE = re.compile(r"\APP_NODE_AUTH_[A-Z0-9_]{1,96}\Z")
_MAX_FILE_BYTES = 64 * 1024
_SCHEMA = "private-node-endpoints/v1"


@dataclass(frozen=True, repr=False)
class RuntimePrivateNodeEndpointResolver:
    """In-memory projection of a permission-checked server private file."""

    _endpoints: Mapping[str, PrivateNodeEndpoint]

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, object] | None = None,
    ) -> RuntimePrivateNodeEndpointResolver:
        """Read a no-secret endpoint file and resolve auth values from env only."""

        env = os.environ if environ is None else environ
        configured = env.get("PP_NODE_PRIVATE_ENDPOINTS_FILE")
        if not isinstance(configured, str) or not configured.strip():
            raise NodeGovernanceError("node_private_runtime_config_missing")
        path = Path(configured).expanduser()
        if not path.is_absolute() or path.is_symlink():
            raise NodeGovernanceError("node_private_runtime_config_invalid")
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise NodeGovernanceError("node_private_runtime_config_invalid")
            if os.name != "nt" and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise NodeGovernanceError("node_private_runtime_config_permissions")
            if metadata.st_size < 1 or metadata.st_size > _MAX_FILE_BYTES:
                raise NodeGovernanceError("node_private_runtime_config_invalid")
            raw = path.read_bytes()
        except NodeGovernanceError:
            raise
        except OSError as exc:
            raise NodeGovernanceError("node_private_runtime_config_unavailable") from exc
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise NodeGovernanceError("node_private_runtime_config_invalid") from None
        return cls(_parse_document(document, env))

    def resolve(self, node_id: str) -> PrivateNodeEndpoint:
        _identifier(node_id, "node_private_endpoint_node_invalid")
        endpoint = self._endpoints.get(node_id)
        if endpoint is None:
            raise NodeGovernanceError("node_private_endpoint_unconfigured")
        return endpoint


def _parse_document(
    document: object,
    environ: Mapping[str, object],
) -> dict[str, PrivateNodeEndpoint]:
    if not isinstance(document, Mapping) or set(document) != {"schema", "nodes"}:
        raise NodeGovernanceError("node_private_runtime_config_invalid")
    if document.get("schema") != _SCHEMA:
        raise NodeGovernanceError("node_private_runtime_config_invalid")
    entries = document.get("nodes")
    if not isinstance(entries, list) or not entries or len(entries) > 64:
        raise NodeGovernanceError("node_private_runtime_config_invalid")
    endpoints: dict[str, PrivateNodeEndpoint] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise NodeGovernanceError("node_private_runtime_config_invalid")
        keys = set(entry)
        if keys != {"node_id", "transport_id", "base_url", "authorization_env"}:
            raise NodeGovernanceError("node_private_runtime_config_invalid")
        node_id = _identifier(entry.get("node_id"), "node_private_runtime_config_invalid")
        transport_id = _identifier(entry.get("transport_id"), "node_private_runtime_config_invalid")
        base_url = entry.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            raise NodeGovernanceError("node_private_runtime_config_invalid")
        auth_env = entry.get("authorization_env")
        if not isinstance(auth_env, str) or _ENV_NAME_RE.fullmatch(auth_env) is None:
            raise NodeGovernanceError("node_private_runtime_config_invalid")
        secret = environ.get(auth_env)
        if not valid_private_node_authorization(secret):
            raise NodeGovernanceError("node_private_runtime_auth_unavailable")
        if node_id in endpoints:
            raise NodeGovernanceError("node_private_runtime_config_invalid")
        try:
            endpoints[node_id] = PrivateNodeEndpoint(
                node_id=node_id,
                transport_id=transport_id,
                base_url=base_url,
                authorization=secret,
            )
        except NodeGovernanceError as exc:
            raise NodeGovernanceError("node_private_runtime_config_invalid") from exc
    return endpoints


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise NodeGovernanceError(code)
    return value
