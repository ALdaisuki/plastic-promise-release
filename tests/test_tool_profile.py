"""Tool surface profile gating tests (audit 2026-10-02)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plastic_promise.mcp.tool_profile import (  # noqa: E402
    ALIAS_MERGE_HIDDEN,
    ALIAS_TO_CANONICAL,
    CORE_KEEP,
    INTERNAL_LIVE,
    active_profile,
    filter_tools,
    is_hidden,
)


def test_default_profile_is_core():
    assert active_profile({}) == "core"
    assert active_profile({"PP_TOOL_PROFILE": ""}) == "core"
    assert active_profile({"PP_TOOL_PROFILE": "FULL"}) == "full"
    assert active_profile({"PP_TOOL_PROFILE": "bogus"}) == "core"


def test_core_keeps_used_and_core_capability():
    names = ["memory_store", "task_verify", "pack_export", "market_status"]
    assert filter_tools(names, "core") == ["memory_store", "task_verify"]


def test_full_keeps_everything_once():
    names = ["memory_store", "pack_export", "market_status", "pack_export"]
    assert filter_tools(names, "full") == ["memory_store", "pack_export", "market_status"]


def test_alias_collapses_to_canonical():
    assert filter_tools(["session_init", "session-init"], "core") == ["session-init"]
    assert ALIAS_TO_CANONICAL["step_closure"] == "step-closure"


def test_hidden_semantics():
    assert is_hidden("pack_export", "core") is True
    assert is_hidden("pack_export", "full") is False
    assert is_hidden("memory_recall", "core") is False
    # 归档面确实仍在 CORE_KEEP 之外
    assert "pack_export" not in CORE_KEEP and "market_status" not in CORE_KEEP


def test_internal_live_hidden_but_dispatchable():
    """T1: zero telemetry does not mean dead code.

    Engine-invoked atom surfaces stay invisible yet dispatchable (fail-open),
    and reappear under the full profile.
    """
    names = sorted(INTERNAL_LIVE)
    assert filter_tools(names, "core") == []
    assert set(filter_tools(names, "full")) == set(names)
    for n in names:
        assert is_hidden(n, "core") is True and is_hidden(n, "full") is False


def test_alias_merge_surfaces_hidden():
    assert filter_tools(sorted(ALIAS_MERGE_HIDDEN), "core") == []
    assert "memory_recall" in CORE_KEEP and "task_verify" in CORE_KEEP
