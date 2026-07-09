"""Default process environment for local Plastic Promise runtimes."""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_PROJECT_ID = "project:plastic-promise"


def configure_default_environment(project_root: str | os.PathLike[str]) -> None:
    """Set default runtime paths and project identity for child/direct services."""
    root = Path(project_root)
    os.environ.setdefault(
        "PLASTIC_DB_PATH",
        str(root / "data" / "db" / "plastic_memory.db"),
    )
    os.environ.setdefault("PLASTIC_LANCEDB_PATH", str(root / "data" / "lancedb"))
    os.environ.setdefault("EMBEDDER_TIMEOUT", "30")
    if "PLASTIC_PROJECT_ID" not in os.environ and "PP_PROJECT_ID" not in os.environ:
        os.environ["PLASTIC_PROJECT_ID"] = DEFAULT_PROJECT_ID
