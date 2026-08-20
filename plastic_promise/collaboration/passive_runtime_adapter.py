"""Narrow server adapter for the PR3 passive-collaboration bridge.

PR3 owns the typed, side-effect-free compiler and the bounded opaque-reference
bridge in :mod:`passive_bridge`.  This module is the only server-wiring seam
that knows how the current process exposes its canonical SQLite writer.  It
deliberately keeps that knowledge out of MCP tool handlers and does not create
any durable AgentRegistry, ProjectWorkBoard, lifecycle record, or restart-safe
cursor.  Those responsibilities belong to PR5.

The adapter is intentionally small: it binds one process-local bridge to the
server's already-owned connection and serialises event appends through the
server's existing write lock.  The bridge receives only the opaque append
callable; MCP callers never receive the connection, event log, lock, or event
constructor.
"""

from __future__ import annotations

from typing import Any

from .event_log import CollaborationEventLog
from .passive_bridge import (
    PassiveCollaborationRuntime,
    open_server_passive_collaboration_runtime,
)

_PASSIVE_COLLABORATION_RUNTIME_ATTR = "_server_passive_collaboration_runtime"


def get_server_passive_collaboration_runtime(
    engine: Any,
) -> PassiveCollaborationRuntime | None:
    """Return the process-local PR3 bridge bound to one canonical server.

    ``engine`` is an internal server dependency, not an MCP payload.  The
    adapter performs the one sanctioned lookup of the server-owned SQLite
    connection, batch context, and write lock.  The public tool layer only
    sees the bridge's opaque reference and publication methods.

    A missing writer is reported as ``None`` so edge/test engines can keep the
    collaboration plane fail-open.  Invalid pre-existing bindings fail closed
    rather than silently replacing another server runtime.
    """

    existing = getattr(engine, _PASSIVE_COLLABORATION_RUNTIME_ATTR, None)
    if existing is not None:
        if not isinstance(existing, PassiveCollaborationRuntime):
            raise RuntimeError("passive_collaboration_runtime_binding_invalid")
        return existing

    storage = getattr(engine, "_sqlite", None)
    connection = getattr(storage, "_conn", None)
    batch = getattr(storage, "batch", None)
    write_lock = getattr(engine, "_write_lock", None)
    if connection is None or not callable(batch) or write_lock is None:
        return None

    with write_lock:
        existing = getattr(engine, _PASSIVE_COLLABORATION_RUNTIME_ATTR, None)
        if existing is not None:
            if not isinstance(existing, PassiveCollaborationRuntime):
                raise RuntimeError("passive_collaboration_runtime_binding_invalid")
            return existing
        with batch():
            event_log = CollaborationEventLog(connection=connection)

        def append_event(event):
            with write_lock, batch():
                return event_log.append(event)

        runtime = open_server_passive_collaboration_runtime(append_event=append_event)
        setattr(engine, _PASSIVE_COLLABORATION_RUNTIME_ATTR, runtime)
        return runtime


__all__ = ["get_server_passive_collaboration_runtime"]
