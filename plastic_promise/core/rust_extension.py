"""Load the optional Rust context-engine extension from installed or source builds.

The source checkout keeps the PyO3 artifact under ``rust/context-engine-core``.  A
running MCP process is often launched from another working directory, so relying on
the process ``PYTHONPATH`` makes the Rust capability appear randomly unavailable.
This loader first honors a normal installed module and then adds the local release
directory without changing any persistent state.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


MODULE_NAME = "context_engine_core"


def _candidate_release_dirs() -> tuple[Path, ...]:
    configured = str(os.environ.get("PP_RUST_EXTENSION_DIR") or "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.extend(Path(part).expanduser() for part in configured.split(os.pathsep))

    # ``core/rust_extension.py`` lives two levels below the source root.
    source_root = Path(__file__).resolve().parents[2]
    candidates.append(source_root / "rust" / "context-engine-core" / "target" / "release")

    # Keep order stable while tolerating duplicate paths with different spellings.
    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen or not resolved.is_dir():
            continue
        seen.add(key)
        result.append(resolved)
    return tuple(result)


def ensure_rust_extension_path() -> Path | None:
    """Put the first directory containing a Rust artifact on ``sys.path``."""

    for directory in _candidate_release_dirs():
        importable = any(
            path.is_file()
            for suffix in importlib.machinery.EXTENSION_SUFFIXES
            for path in directory.glob(f"context_engine_core*{suffix}")
        )
        if not importable:
            continue
        directory_text = str(directory)
        if directory_text not in sys.path:
            sys.path.insert(0, directory_text)
        return directory
    return None


def load_context_engine_core() -> ModuleType:
    """Import ``context_engine_core`` or raise a normal ``ImportError``."""

    try:
        return importlib.import_module(MODULE_NAME)
    except (ImportError, ModuleNotFoundError) as first_error:
        if MODULE_NAME in sys.modules:
            sys.modules.pop(MODULE_NAME, None)
        if ensure_rust_extension_path() is None:
            raise first_error
        try:
            return importlib.import_module(MODULE_NAME)
        except (ImportError, ModuleNotFoundError):
            raise first_error from None


def try_load_context_engine_core() -> ModuleType | None:
    """Best-effort variant used by health probes and optional feature paths."""

    try:
        return load_context_engine_core()
    except Exception:
        return None


__all__ = [
    "MODULE_NAME",
    "ensure_rust_extension_path",
    "load_context_engine_core",
    "try_load_context_engine_core",
]
