"""Re-registration revives a stale agent session (work-board keepalive fix).

The durable collaboration plane marks an AgentSession 'stale' after the
presence window lapses.  Before this fix no public path could return a
stale row to 'active', deadlocking every work-board operation for that
transport.  Re-registration with the exact verified identity IS the
revival channel: re-authentication semantics, not resurrection of an
unverified row.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.collaboration.contracts import AgentIdentity, ProjectScope
from plastic_promise.collaboration.policy_binding import (
    open_server_agent_policy_binding_authority,
)
from plastic_promise.collaboration.durable_runtime import (
    DurableCollaborationError,
    DurableCollaborationRuntime,
)
from plastic_promise.collaboration.runtime_binding import AgentSession
from tests.pr5_schema_fixture import install_pr5_collaboration_schema

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


class _Tx:
    def __init__(self, conn):
        self._conn = conn

    def __call__(self):
        self._conn.execute("BEGIN IMMEDIATE")
        return self

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False


@pytest.fixture()
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    connection.execute("PRAGMA foreign_keys = ON")
    install_pr5_collaboration_schema(
        connection,
        transaction_factory=_Tx(connection),
        clock=lambda: NOW,
        suffix="stale-revive",
    )
    yield connection
    connection.close()


def _session(session_id="agent-session:t1"):
    return AgentSession(
        session_id=session_id,
        identity=AgentIdentity("agent:claude", "participant"),
        project=ProjectScope("project:smoke"),
        coordination_session_id="coord:smoke",
        state="active",
        started_at=NOW.isoformat().replace("+00:00", "Z"),
        last_heartbeat_at=NOW.isoformat().replace("+00:00", "Z"),
        expires_at=(NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
    )


def _row_state(conn, session_id):
    return str(
        conn.execute(
            "SELECT state FROM collaboration_agent_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()[0]
    )


def test_register_session_revives_stale_row(conn):
    runtime = DurableCollaborationRuntime(
        conn,
        transaction_factory=_Tx(conn),
        clock=lambda: NOW,
        policy_authority=open_server_agent_policy_binding_authority(clock=lambda: NOW),
    )
    session = _session()
    runtime.register_session(session)
    assert _row_state(conn, session.session_id) == "active"

    conn.execute(
        "UPDATE collaboration_agent_sessions SET state='stale' WHERE session_id=?",
        (session.session_id,),
    )
    conn.commit()

    result = runtime.register_session(session)

    assert _row_state(conn, session.session_id) == "active"
    assert result.session.state == "active"


def test_register_session_still_rejects_closed_row(conn):
    runtime = DurableCollaborationRuntime(
        conn,
        transaction_factory=_Tx(conn),
        clock=lambda: NOW,
        policy_authority=open_server_agent_policy_binding_authority(clock=lambda: NOW),
    )
    session = _session("agent-session:t2")
    runtime.register_session(session)
    conn.execute(
        "UPDATE collaboration_agent_sessions SET state='closed' WHERE session_id=?",
        (session.session_id,),
    )
    conn.commit()
    with pytest.raises(DurableCollaborationError):
        runtime.register_session(session)
