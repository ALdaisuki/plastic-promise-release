"""Shared runtime mode configuration for launcher and MCP hot updates."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, MutableMapping


@dataclass(frozen=True)
class RuntimeMode:
    key: str
    label: str
    depth: str
    rust_accelerated: bool
    description: str

    @property
    def runs_lancedb_warmup(self) -> bool:
        return self.depth == "full"


RUNTIME_MODES = (
    RuntimeMode(
        key="light",
        label="轻量",
        depth="light",
        rust_accelerated=False,
        description="Fast Python path; LanceDB init and startup warmup are deferred.",
    ),
    RuntimeMode(
        key="normal",
        label="普通",
        depth="normal",
        rust_accelerated=False,
        description="Python path with LanceDB available through lazy init, no startup rebuild.",
    ),
    RuntimeMode(
        key="rust-normal",
        label="Rust加速版普通",
        depth="normal",
        rust_accelerated=True,
        description="Rust-first supply with Python fallback, no startup rebuild.",
    ),
    RuntimeMode(
        key="full",
        label="完全",
        depth="full",
        rust_accelerated=False,
        description="Python path plus startup LanceDB warmup/backfill/rebuild.",
    ),
    RuntimeMode(
        key="rust-full",
        label="Rust加速版完全",
        depth="full",
        rust_accelerated=True,
        description="Rust-first supply plus startup LanceDB warmup/backfill/rebuild.",
    ),
)

_MODES_BY_KEY = {mode.key: mode for mode in RUNTIME_MODES}
_ALIASES = {
    "lite": "light",
    "轻量": "light",
    "普通": "normal",
    "rust_normal": "rust-normal",
    "rust-normal": "rust-normal",
    "rust普通": "rust-normal",
    "rust加速版普通": "rust-normal",
    "rust加速普通": "rust-normal",
    "完全": "full",
    "complete": "full",
    "rust_full": "rust-full",
    "rust-full": "rust-full",
    "rust完全": "rust-full",
    "rust加速版完全": "rust-full",
    "rust加速完全": "rust-full",
}

RUNTIME_MODE_KEYS = tuple(mode.key for mode in RUNTIME_MODES)
RUNTIME_MODE_LABELS = tuple(mode.label for mode in RUNTIME_MODES)


def normalize_runtime_mode(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    lowered = normalized.lower().replace(" ", "").replace("_", "-")
    return _ALIASES.get(normalized) or _ALIASES.get(lowered) or lowered


def get_runtime_mode(value: str | None) -> RuntimeMode:
    key = normalize_runtime_mode(value)
    if key in _MODES_BY_KEY:
        return _MODES_BY_KEY[key]
    valid = ", ".join(RUNTIME_MODE_KEYS)
    raise ValueError(f"Unknown runtime mode '{value}'. Valid modes: {valid}")


def _set_env(environ: MutableMapping[str, str], key: str, value: str) -> None:
    environ[key] = value


def apply_runtime_mode(
    mode: str | RuntimeMode,
    environ: MutableMapping[str, str] | None = None,
) -> RuntimeMode:
    """Apply mode switches to process environment and return the resolved mode."""
    resolved = mode if isinstance(mode, RuntimeMode) else get_runtime_mode(mode)
    target_env = environ if environ is not None else os.environ

    _set_env(target_env, "PLASTIC_RUNTIME_MODE", resolved.key)
    _set_env(target_env, "PLASTIC_RUNTIME_DEPTH", resolved.depth)

    if resolved.rust_accelerated:
        _set_env(target_env, "PP_FORCE_PYTHON_SUPPLY", "0")
        _set_env(target_env, "PP_PREFER_RUST_SUPPLY", "1")
    else:
        _set_env(target_env, "PP_FORCE_PYTHON_SUPPLY", "1")
        _set_env(target_env, "PP_PREFER_RUST_SUPPLY", "0")

    if resolved.depth == "light":
        _set_env(target_env, "LDB_INIT_ON_HEAVY_INIT", "0")
        _set_env(target_env, "LDB_BACKFILL_ON_INIT", "0")
        _set_env(target_env, "LDB_REBUILD_ON_INIT", "0")
        _set_env(target_env, "PLASTIC_SKIP_LANCEDB_WARMUP", "1")
    elif resolved.depth == "normal":
        _set_env(target_env, "LDB_INIT_ON_HEAVY_INIT", "1")
        _set_env(target_env, "LDB_BACKFILL_ON_INIT", "0")
        _set_env(target_env, "LDB_REBUILD_ON_INIT", "0")
        _set_env(target_env, "PLASTIC_SKIP_LANCEDB_WARMUP", "1")
    else:
        _set_env(target_env, "LDB_INIT_ON_HEAVY_INIT", "1")
        _set_env(target_env, "LDB_BACKFILL_ON_INIT", "1")
        _set_env(target_env, "LDB_REBUILD_ON_INIT", "1")
        _set_env(target_env, "PLASTIC_SKIP_LANCEDB_WARMUP", "0")

    return resolved


def infer_runtime_mode(environ: Mapping[str, str] | None = None) -> RuntimeMode:
    env = environ if environ is not None else os.environ
    configured = normalize_runtime_mode(env.get("PLASTIC_RUNTIME_MODE"))
    if configured in _MODES_BY_KEY:
        return _MODES_BY_KEY[configured]

    rust_accelerated = (
        env.get("PP_FORCE_PYTHON_SUPPLY") != "1"
        and env.get("PP_PREFER_RUST_SUPPLY", "1") == "1"
    )
    depth = env.get("PLASTIC_RUNTIME_DEPTH")
    if depth not in {"light", "normal", "full"}:
        if env.get("PLASTIC_SKIP_LANCEDB_WARMUP") == "1":
            depth = "normal" if env.get("LDB_INIT_ON_HEAVY_INIT") == "1" else "light"
        elif env.get("LDB_BACKFILL_ON_INIT") == "1" or env.get("LDB_REBUILD_ON_INIT") == "1":
            depth = "full"
        else:
            depth = "normal"

    if depth == "full":
        return _MODES_BY_KEY["rust-full" if rust_accelerated else "full"]
    if rust_accelerated:
        return _MODES_BY_KEY["rust-normal"]
    return _MODES_BY_KEY["light" if depth == "light" else "normal"]


def runtime_mode_status(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    env = environ if environ is not None else os.environ
    mode = infer_runtime_mode(env)
    return {
        "mode": mode.key,
        "label": mode.label,
        "depth": mode.depth,
        "rust_accelerated": mode.rust_accelerated,
        "runs_lancedb_warmup": mode.runs_lancedb_warmup,
        "env": {
            "PP_FORCE_PYTHON_SUPPLY": env.get("PP_FORCE_PYTHON_SUPPLY"),
            "PP_PREFER_RUST_SUPPLY": env.get("PP_PREFER_RUST_SUPPLY"),
            "LDB_INIT_ON_HEAVY_INIT": env.get("LDB_INIT_ON_HEAVY_INIT"),
            "LDB_BACKFILL_ON_INIT": env.get("LDB_BACKFILL_ON_INIT"),
            "LDB_REBUILD_ON_INIT": env.get("LDB_REBUILD_ON_INIT"),
            "PLASTIC_SKIP_LANCEDB_WARMUP": env.get("PLASTIC_SKIP_LANCEDB_WARMUP"),
        },
    }


def select_runtime_mode(
    explicit_mode: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
    interactive: bool | None = None,
    default_interactive: str = "normal",
    default_non_interactive: str = "rust-full",
) -> RuntimeMode:
    """Resolve startup mode from CLI/env/prompt.

    Non-interactive launch keeps the previous Rust-first full-start behavior by default.
    """
    env = environ if environ is not None else os.environ
    if explicit_mode:
        return get_runtime_mode(explicit_mode)

    env_mode = env.get("PLASTIC_RUNTIME_MODE")
    if env_mode:
        return get_runtime_mode(env_mode)

    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    if not is_interactive:
        return get_runtime_mode(default_non_interactive)

    default_mode = get_runtime_mode(default_interactive)
    print_func("")
    print_func("请选择 Plastic Promise 启动模式：")
    for idx, mode in enumerate(RUNTIME_MODES, start=1):
        default_mark = " (默认)" if mode.key == default_mode.key else ""
        print_func(f"  {idx}. {mode.label} [{mode.key}]{default_mark} - {mode.description}")

    while True:
        answer = input_func("模式编号或名称> ").strip()
        if not answer:
            return default_mode
        if answer.isdigit():
            index = int(answer) - 1
            if 0 <= index < len(RUNTIME_MODES):
                return RUNTIME_MODES[index]
        try:
            return get_runtime_mode(answer)
        except ValueError:
            valid = ", ".join(RUNTIME_MODE_KEYS)
            print_func(f"无效模式：{answer}。可选：{valid}")
