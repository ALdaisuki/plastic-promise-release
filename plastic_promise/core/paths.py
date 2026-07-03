"""Shared filesystem paths for Plastic Promise.

Keep defaults stable when modules are run directly from different working
directories. Environment variables still take precedence.
"""

from __future__ import annotations

import os
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """Return the repository root for this installed source tree."""
    return _PROJECT_ROOT


def default_db_path() -> str:
    """Return the canonical SQLite database path."""
    return str(_PROJECT_ROOT / "data" / "db" / "plastic_memory.db")


def get_db_path() -> str:
    """Return PLASTIC_DB_PATH or the canonical project database path."""
    return os.environ.get("PLASTIC_DB_PATH", default_db_path())
