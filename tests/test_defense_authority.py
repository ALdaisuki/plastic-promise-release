"""Authority isolation tests for ``handle_defense`` dispatch paths.

Covers the server-dispatch contract of ``defense``:

1. Public adjust from a non-reviewer actor is rejected
   (defense_reviewer_authority_required) and writes nothing.
2. Public adjust from claude succeeds and the recorded reason
   carries the "[claude] " attribution prefix visible via
   TrustManager.history.
3. The trusted in-process path (_runtime_context=None, scanners
   and daemons) keeps its historical adjust behavior unchanged.
4. Public evaluate_tool ignores caller-supplied trust_score /
   trust_tier overrides and always grades against the values held
   by TrustManager.

Isolation follows tests/test_trust_store.py: each test swaps the
module-level singleton for a fresh TrustManager backed by a temporary
TrustStore database, so the real plastic_memory.db is never read
or written.
"""

import asyncio
import json
import os
import tempfile

import pytest

import plastic_promise.mcp.tools.audit_defense as audit_defense
from plastic_promise.defense.soul_enforcer import TrustManager
from plastic_promise.defense.trust_store import TrustStore
from plastic_promise.mcp.tools.audit_defense import handle_defense

SERVER_DISPATCH = {"authority_source": "server_dispatch"}


@pytest.fixture
def tm():
    """Fresh TrustManager over a temp DB, installed as module singleton."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    manager = TrustManager(trust_store=TrustStore(db_path=path))
    saved = audit_defense._trust_manager
    audit_defense._trust_manager = manager
    yield manager
    audit_defense._trust_manager = saved
    manager._store._conn.close()
    try:
        os.unlink(path)
    except PermissionError:
        pass  # Windows may hold the file briefly


def _invoke(args, runtime_context=None):
    """Run handle_defense through asyncio.run; return the parsed payload."""
    result = asyncio.run(handle_defense(None, args, _runtime_context=runtime_context))
    assert isinstance(result, list) and result, "handler must return TextContent"
    return json.loads(result[0].text)


class TestDefenseAuthority:
    def test_public_adjust_non_reviewer_actor_rejected(self, tm):
        """(1) Non-reviewer actor via public dispatch cannot move trust."""
        payload = _invoke(
            {
                "action": "adjust",
                "delta": 0.05,
                "reason": "self-promotion attempt",
                "target": "auth_target_a",
            },
            runtime_context={"actor": "pi_builder", **SERVER_DISPATCH},
        )
        assert payload["success"] is False
        assert payload["error"] == "defense_reviewer_authority_required"

        # Nothing may have been written for the attempted target.
        assert tm.history("auth_target_a") == []
        assert tm.get("auth_target_a") == pytest.approx(0.60)

    def test_claude_public_adjust_succeeds_with_prefix(self, tm):
        """(2) claude adjusts publicly; history shows the "[claude] " prefix."""
        payload = _invoke(
            {
                "action": "adjust",
                "delta": 0.02,
                "reason": "acceptance reward",
                "target": "auth_target_b",
            },
            runtime_context={"actor": "claude", **SERVER_DISPATCH},
        )
        assert "error" not in payload
        assert payload["action"] == "adjust"

        history = tm.history("auth_target_b")
        assert history, "successful adjust must appear in TrustManager.history"
        assert history[-1]["reason"].startswith("[claude] ")

    def test_internal_path_adjust_behavior_unchanged(self, tm):
        """(3) _runtime_context=None (in-process scanners/daemons) intact."""
        payload = _invoke(
            {
                "action": "adjust",
                "delta": -0.03,
                "reason": "user rejection",
                "target": "auth_target_c",
            },
            runtime_context=None,
        )
        assert "error" not in payload
        assert payload["action"] == "adjust"
        assert payload["new_trust"] == pytest.approx(0.57)

        history = tm.history("auth_target_c")
        assert history[-1]["reason"] == "user rejection"
        assert history[-1]["direction"] == "decay"

    def test_public_evaluate_tool_ignores_trust_overrides(self, tm):
        """(4) Public evaluate_tool grades stored trust, not args overrides."""
        tm.adjust(0.25, "seed trust", target="auth_target_d")  # 0.85 / high

        payload = _invoke(
            {
                "action": "evaluate_tool",
                "tool_name": "memory_gc",  # critical, requires 0.80
                "target": "auth_target_d",
                "trust_score": 0.0,  # forged override must be ignored
                "trust_tier": "critical",
            },
            runtime_context={"actor": "pi_builder", **SERVER_DISPATCH},
        )
        assert payload["tool_name"] == "memory_gc"
        assert payload["trust_score"] == pytest.approx(0.85)
        assert payload["trust_tier"] == "high"
        assert payload["decision"] == "ask"
        assert "trust_below_hard_minimum" not in payload["reasons"]
