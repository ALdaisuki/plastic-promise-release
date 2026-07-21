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

    configured_chunking = str(target_env.get("PP_MEMORY_CHUNKING") or "off").strip().casefold()
    if resolved.depth == "full":
        effective_chunking = "structure-v1"
    else:
        effective_chunking = "shadow" if configured_chunking == "shadow" else "off"
    _set_env(target_env, "PP_MEMORY_CHUNKING", effective_chunking)
    _set_env(
        target_env,
        "PP_MEMORY_CHUNK_ENGINE",
        "rust" if resolved.rust_accelerated else "python",
    )

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
        _set_env(target_env, "LDB_BACKFILL_ON_INIT", "0")
        _set_env(target_env, "LDB_REBUILD_ON_INIT", "0")
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
        "chunking": _structured_chunking_status(env),
        "env": {
            "PP_FORCE_PYTHON_SUPPLY": env.get("PP_FORCE_PYTHON_SUPPLY"),
            "PP_PREFER_RUST_SUPPLY": env.get("PP_PREFER_RUST_SUPPLY"),
            "LDB_INIT_ON_HEAVY_INIT": env.get("LDB_INIT_ON_HEAVY_INIT"),
            "LDB_BACKFILL_ON_INIT": env.get("LDB_BACKFILL_ON_INIT"),
            "LDB_REBUILD_ON_INIT": env.get("LDB_REBUILD_ON_INIT"),
            "PLASTIC_SKIP_LANCEDB_WARMUP": env.get("PLASTIC_SKIP_LANCEDB_WARMUP"),
            "PP_MEMORY_CHUNKING": env.get("PP_MEMORY_CHUNKING"),
            "PP_MEMORY_CHUNK_ENGINE": env.get("PP_MEMORY_CHUNK_ENGINE"),
        },
    }


def _structured_chunking_status(env: Mapping[str, str]) -> dict[str, object]:
    configured_mode = str(env.get("PP_MEMORY_CHUNKING") or "off").strip().casefold()
    requested_engine = str(env.get("PP_MEMORY_CHUNK_ENGINE") or "python").strip().casefold()
    status: dict[str, object] = {
        "schema_version": "structure-v1",
        "configured_mode": configured_mode,
        "effective_mode": "off",
        "requested_engine": requested_engine,
        "effective_engine": "disabled",
        "enabled": False,
        "parity_status": "not_applicable",
        "capability_reason": "configured_off",
    }
    if configured_mode != "structure-v1":
        if configured_mode not in {"off", "shadow"}:
            status["capability_reason"] = "invalid_chunking_mode"
        return status

    status.update(
        {
            "effective_mode": "structure-v1",
            "effective_engine": "python",
            "enabled": True,
            "rust_artifact": _rust_artifact_identity() if requested_engine == "rust" else None,
        }
    )
    if requested_engine != "rust":
        status.update(
            {
                "parity_status": "not_required",
                "capability_reason": "python_structure_v1_active",
            }
        )
        return status

    parity = _rust_chunking_parity()
    status["parity_status"] = parity["status"]
    status["capability_reason"] = parity["reason"]
    if parity["status"] == "matched":
        status["effective_engine"] = "rust"
    return status


def _rust_chunking_parity() -> dict[str, str]:
    try:
        from plastic_promise.core.rust_extension import load_context_engine_core

        rust_core = load_context_engine_core()
        projection = getattr(rust_core, "structure_chunk_projection", None)
        if not callable(projection):
            return {"status": "unavailable", "reason": "rust_chunking_api_unavailable"}
        from plastic_promise.core.chunking import (
            STRUCTURE_CHUNK_PARITY_PROBE,
            build_chunk_manifest,
        )

        sample = STRUCTURE_CHUNK_PARITY_PROBE
        expected = build_chunk_manifest(
            sample,
            target_chars=32,
            hard_chars=64,
            max_chunks=16,
        )["chunks"]
        actual = projection(sample, 32, 64, 16)
        fields = (
            "chunk_id",
            "ordinal",
            "kind",
            "source_start",
            "source_end",
            "source_hash",
            "text_hash",
            "text",
            "context_truncated",
        )
        expected_rows = [
            {
                **{field: row.get(field) for field in fields},
                "heading_path": row.get("header_path", []),
            }
            for row in expected
        ]
        actual_rows = [
            {
                **{field: row.get(field) for field in fields},
                "heading_path": row.get("heading_path", row.get("header_path", [])),
            }
            for row in actual
            if isinstance(row, dict)
        ]
        if actual_rows != expected_rows:
            return {"status": "mismatch", "reason": "rust_python_chunking_mismatch"}
        return {"status": "matched", "reason": "rust_python_chunking_parity_matched"}
    except Exception:
        return {"status": "unavailable", "reason": "rust_chunking_probe_failed"}


def _rust_artifact_identity() -> dict[str, object]:
    """Expose the exact optional Rust artifact used by the health probe."""
    try:
        from plastic_promise.core.rust_extension import load_context_engine_core

        module = load_context_engine_core()
        path = getattr(module, "__file__", None)
        identity: dict[str, object] = {
            "module": getattr(module, "__name__", "context_engine_core"),
            "version": getattr(module, "__version__", None),
            "path": str(path) if path else "",
        }
        if path:
            from pathlib import Path

            artifact = Path(path)
            stat = artifact.stat()
            identity.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        return identity
    except Exception as exc:
        return {"module": "context_engine_core", "error": exc.__class__.__name__}


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
