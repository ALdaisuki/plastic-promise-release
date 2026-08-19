"""Default process environment for local Plastic Promise runtimes."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PROJECT_ID = "project:plastic-promise"


def configure_default_environment(project_root: str | os.PathLike[str]) -> None:
    """Set safe, visible defaults for child/direct services.

    The launcher is the user-facing composition boundary.  Keep the feature
    switches here instead of scattering implicit defaults across providers so
    a fresh install exposes the Dashboard and the governed semantic pipeline
    consistently.  Operators can still override every value before startup.
    """
    root = Path(project_root)
    os.environ.setdefault(
        "PLASTIC_DB_PATH",
        str(root / "data" / "db" / "plastic_memory.db"),
    )
    os.environ.setdefault("PLASTIC_LANCEDB_PATH", str(root / "data" / "lancedb"))
    os.environ.setdefault("EMBEDDER_TIMEOUT", "10")
    # The local operator surface is intentionally visible on first launch.
    # It remains loopback-only and project-scoped; review actions stay opt-in.
    os.environ.setdefault("PP_DASHBOARD_V2", "1")
    os.environ.setdefault("PP_RETRIEVAL_EXPLAIN", "1")

    # Deterministic structure-v1 slicing is the canonical baseline.  Semantic
    # enrichment/knowledge compilation run in bounded shadow mode by default:
    # they are observable and queue work without rewriting source chunks or
    # promoting unverified model output.  A configured cloud provider can be
    # promoted to ``on`` through the Dashboard's controlled configuration.
    os.environ.setdefault("PP_MEMORY_CHUNKING", "structure-v1")
    os.environ.setdefault("PP_MEMORY_CHUNK_ENRICHMENT", "shadow")
    os.environ.setdefault("PP_MEMORY_CHUNK_ENRICHMENT_PROVIDER", "openai-compatible")
    os.environ.setdefault("PP_KNOWLEDGE_SYSTEM", "on")
    os.environ.setdefault("PP_KNOWLEDGE_SEMANTIC", "shadow")

    # Passive capture and semantic route classification are project-scoped,
    # bounded, and proposal-only; enabling them does not write canonical
    # memory without the existing promotion gates.
    os.environ.setdefault("PP_PASSIVE_CONTEXT", "on")
    os.environ.setdefault("PP_PASSIVE_MEMORY", "on")
    os.environ.setdefault("PP_PASSIVE_TOOL_ROUTING", "on")
    os.environ.setdefault("PP_PASSIVE_SEMANTIC_CAPTURE", "shadow")
    os.environ.setdefault("PP_PASSIVE_SEMANTIC_ROUTING", "shadow")
    if "PLASTIC_PROJECT_ID" not in os.environ and "PP_PROJECT_ID" not in os.environ:
        os.environ["PLASTIC_PROJECT_ID"] = DEFAULT_PROJECT_ID
