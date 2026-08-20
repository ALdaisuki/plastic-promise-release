"""Single execution-plane gate for provider construction."""

from __future__ import annotations

import os

COMPUTE_NODE_ROLE = "pp-compute-node"


def current_endpoint_role() -> str:
    return os.environ.get("PP_ENDPOINT_ROLE", "").strip()


def require_compute_node_role(*, injected_transport: bool = False) -> None:
    """Fail closed unless a production provider runs on the compute node.

    Injected transports are reserved for deterministic tests/private adapters;
    real network clients must declare the compute role explicitly.
    """

    role = current_endpoint_role()
    if role == COMPUTE_NODE_ROLE:
        return
    # Deterministic unit/private-adapter tests may inject a transport without
    # pretending to be a deployed endpoint.  Production construction never
    # has an injected transport, so a missing role remains fail-closed.
    if not role and injected_transport:
        return
    raise RuntimeError("inference_requires_compute_node")


__all__ = ["COMPUTE_NODE_ROLE", "current_endpoint_role", "require_compute_node_role"]
