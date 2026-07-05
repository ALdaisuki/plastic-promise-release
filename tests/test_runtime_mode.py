import json
import threading

import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.launcher.runtime_mode import (
    apply_runtime_mode,
    get_runtime_mode,
    runtime_mode_status,
    select_runtime_mode,
)


def test_runtime_mode_env_for_light():
    env = {}

    mode = apply_runtime_mode("light", env)

    assert mode.key == "light"
    assert env["PP_FORCE_PYTHON_SUPPLY"] == "1"
    assert env["PP_PREFER_RUST_SUPPLY"] == "0"
    assert env["LDB_INIT_ON_HEAVY_INIT"] == "0"
    assert env["PLASTIC_SKIP_LANCEDB_WARMUP"] == "1"


def test_runtime_mode_env_for_rust_full():
    env = {}

    mode = apply_runtime_mode("rust-full", env)

    assert mode.key == "rust-full"
    assert env["PP_FORCE_PYTHON_SUPPLY"] == "0"
    assert env["PP_PREFER_RUST_SUPPLY"] == "1"
    assert env["LDB_INIT_ON_HEAVY_INIT"] == "1"
    assert env["LDB_BACKFILL_ON_INIT"] == "1"
    assert env["LDB_REBUILD_ON_INIT"] == "1"
    assert env["PLASTIC_SKIP_LANCEDB_WARMUP"] == "0"


def test_runtime_mode_accepts_chinese_aliases():
    assert get_runtime_mode("普通").key == "normal"
    assert get_runtime_mode("Rust加速版完全").key == "rust-full"


def test_select_runtime_mode_prompts_interactively():
    prompts = []
    mode = select_runtime_mode(
        interactive=True,
        input_func=lambda _: "3",
        print_func=prompts.append,
        environ={},
    )

    assert mode.key == "rust-normal"
    assert any("Plastic Promise" in line for line in prompts)


def test_select_runtime_mode_non_interactive_preserves_rust_full_default():
    mode = select_runtime_mode(interactive=False, environ={})

    assert mode.key == "rust-full"


@pytest.mark.asyncio
async def test_runtime_mode_mcp_set_refreshes_engine(monkeypatch):
    from plastic_promise.mcp.tools.runtime import handle_runtime_mode

    class FakeEngine:
        def __init__(self):
            self.calls = []

        def refresh_runtime_mode(self, initialize_heavy=False):
            self.calls.append(initialize_heavy)

    for key in [
        "PLASTIC_RUNTIME_MODE",
        "PLASTIC_RUNTIME_DEPTH",
        "PP_FORCE_PYTHON_SUPPLY",
        "PP_PREFER_RUST_SUPPLY",
        "LDB_INIT_ON_HEAVY_INIT",
        "LDB_BACKFILL_ON_INIT",
        "LDB_REBUILD_ON_INIT",
        "PLASTIC_SKIP_LANCEDB_WARMUP",
    ]:
        monkeypatch.delenv(key, raising=False)

    engine = FakeEngine()

    result = await handle_runtime_mode(engine, {"action": "set", "mode": "rust-full"})
    data = json.loads(result[0].text)

    assert data["mode"] == "rust-full"
    assert data["rust_accelerated"] is True
    assert data["refresh"]["called"] is True
    assert engine.calls == [True]


def test_runtime_mode_status_reports_applied_mode():
    env = {}
    apply_runtime_mode("normal", env)

    status = runtime_mode_status(env)

    assert status["mode"] == "normal"
    assert status["runs_lancedb_warmup"] is False


def test_runtime_mode_status_matches_rust_first_supply_default():
    status = runtime_mode_status({})

    assert status["mode"] == "rust-normal"


def test_refresh_runtime_mode_light_clears_existing_lancedb(monkeypatch):
    engine = ContextEngine.__new__(ContextEngine)
    engine._heavy_init_lock = threading.RLock()
    engine._heavy_init_done = True
    engine._ldb = object()
    engine.reset_rust_health = lambda: None
    monkeypatch.setenv("LDB_INIT_ON_HEAVY_INIT", "0")

    ContextEngine.refresh_runtime_mode(engine, initialize_heavy=False)

    assert engine._ldb is None
    assert engine._heavy_init_done is True
