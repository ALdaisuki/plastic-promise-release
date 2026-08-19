"""Loopback-only local inference node contract.

The node has no SQLite, LanceDB, MCP, or control-plane write capability.
"""

from .contract import NodeConfigurationError, NodeIdentity, NodeLimits

__all__ = ["NodeConfigurationError", "NodeIdentity", "NodeLimits"]
