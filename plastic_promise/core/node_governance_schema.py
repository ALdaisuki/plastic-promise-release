"""Compatibility export for the deployment-owned node-governance schema.

The runtime can verify this schema but never executes DDL during startup.  The
implementation lives in ``plastic_promise.deployment.sqlite_migrations`` so
the cross-platform deploy controller can apply it without importing runtime
or MCP layers.
"""

from __future__ import annotations

from plastic_promise.deployment.sqlite_migrations import (
    NODE_GOVERNANCE_SCHEMA_VERSION,
    apply_node_governance_schema,
    node_governance_schema_present,
)

__all__ = [
    "NODE_GOVERNANCE_SCHEMA_VERSION",
    "apply_node_governance_schema",
    "node_governance_schema_present",
]
