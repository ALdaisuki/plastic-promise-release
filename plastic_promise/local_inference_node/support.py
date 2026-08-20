"""Small compute-only support seams.

Only policy primitives that are not part of the server package live here.
Provider transport is implemented in the adjacent ``provider_http`` module,
which is the security-preserving relocation of the canonical transport.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

DEFAULT_STRUCTURED_REQUEST_TOKENS = 32 * 1024
UNBOUNDED_STRUCTURED_TOKEN_LIMIT = 0
_AUTHORIZATION_RE = re.compile(r"\ABearer [A-Za-z0-9._~+/=-]{1,4096}\Z")


def structured_tokens_allowed(requested: int, configured_limit: int) -> bool:
    return (
        isinstance(requested, int)
        and not isinstance(requested, bool)
        and requested >= 1
        and isinstance(configured_limit, int)
        and not isinstance(configured_limit, bool)
        and configured_limit >= 0
        and (configured_limit == UNBOUNDED_STRUCTURED_TOKEN_LIMIT or requested <= configured_limit)
    )


def validate_structured_token_limit(value: int, *, allow_unbounded: bool = True) -> int:
    minimum = 0 if allow_unbounded else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError("structured_token_limit_invalid")
    return value


def structured_intent_digest(
    *, project_id: str, intent_id: str, schema_id: str, user_payload: dict[str, object]
) -> str:
    canonical = json.dumps(user_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    material = "\x1f".join((project_id, intent_id, schema_id, canonical))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def valid_private_node_authorization(value: object) -> bool:
    return isinstance(value, str) and _AUTHORIZATION_RE.fullmatch(value) is not None


def require_compute_node_role(*, injected_transport: bool = False) -> None:
    """Fail closed for real provider calls outside the compute endpoint."""

    role = os.environ.get("PP_ENDPOINT_ROLE", "").strip()
    if role == "pp-compute-node":
        return
    if not role and injected_transport:
        return
    raise RuntimeError("inference_requires_compute_node")


__all__ = [
    "DEFAULT_STRUCTURED_REQUEST_TOKENS",
    "UNBOUNDED_STRUCTURED_TOKEN_LIMIT",
    "require_compute_node_role",
    "structured_intent_digest",
    "structured_tokens_allowed",
    "valid_private_node_authorization",
    "validate_structured_token_limit",
]
