"""External seam contracts for the knowledge system deep module.

Callers and tests should import these interfaces and never reach into
internal pipeline stages.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plastic_promise.core.paths import get_db_path

MAX_KNOWN_SOURCE_KINDS = frozenset({"upload", "folder", "git", "url", "server_path"})

_SOURCE_STATE_ROOT_CACHE: dict[str, Path] = {}


def utc_now_iso() -> str:
    """Return a stable UTC ISO-8601 timestamp with milliseconds."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def knowledge_state_root() -> Path:
    """Return the state root used for the knowledge store and blobs."""
    env = os.environ.get("PP_KNOWLEDGE_STATE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    canonical = Path(get_db_path()).expanduser().resolve()
    state_root = canonical.parent.parent if canonical.parent.name == "db" else canonical.parent
    return state_root / "knowledge"


def knowledge_db_path() -> Path:
    """Return the canonical knowledge SQLite database path."""
    env = os.environ.get("PP_KNOWLEDGE_DB_PATH")
    if env:
        return Path(env).expanduser().resolve()
    return knowledge_state_root() / "plastic_knowledge.db"


def knowledge_blob_root() -> Path:
    """Return the canonical content-addressed blob root."""
    env = os.environ.get("PP_KNOWLEDGE_BLOB_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return knowledge_state_root() / "blobs"


def knowledge_feature_gate(flag: str) -> str:
    """Return the effective feature gate value: off | shadow | on (default off)."""
    value = os.environ.get(flag, "off").strip().lower()
    if value not in {"off", "shadow", "on"}:
        return "off"
    return value


@dataclass(frozen=True)
class NormalizedDocument:
    """A parser-neutral projection of a raw source artifact."""

    title: str
    text: str
    parser_id: str
    parse_schema: str
    media_anchors: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class BlobRef:
    """A content-addressed reference to immutable source bytes."""

    sha256: str
    byte_size: int


@dataclass(frozen=True)
class SourceView:
    """Read projection of a knowledge source."""

    id: str
    space_id: str
    project_id: str
    kind: str
    name: str
    origin_ref: str | None
    status: str
    active_version_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class VersionView:
    """Read projection of an immutable source version."""

    id: str
    source_id: str
    version_no: int
    blob_sha256: str
    byte_size: int
    content_hash: str
    parser_id: str
    parse_schema: str
    document_title: str
    status: str
    created_at: str
    superseded_at: str | None
    chunk_count: int = 0


@dataclass(frozen=True)
class JobView:
    """Read projection of a knowledge ingestion job."""

    id: str
    source_id: str | None
    project_id: str
    stage: str
    status: str
    attempts: int
    error: str | None
    result_json: dict[str, Any] | None
    created_at: str
    updated_at: str
    finished_at: str | None


@dataclass(frozen=True)
class Submission:
    """Result of submitting a source for ingestion."""

    job_id: str
    source_id: str
    reused_version_id: str | None
    status: str


@dataclass(frozen=True)
class ActiveProjectCursor:
    """Stable keyset position within the active-project planning order."""

    last_updated: str
    project_id: str


@dataclass(frozen=True)
class ActiveProjectPage:
    """One bounded round-robin page of active knowledge projects."""

    project_ids: tuple[str, ...]
    next_cursor: ActiveProjectCursor | None
    has_more: bool


@dataclass(frozen=True)
class SemanticChunkCursor:
    """Stable keyset position within one project's active Evidence Chunks."""

    created_at: str
    ordinal: int
    row_id: str


@dataclass(frozen=True)
class ChunkHit:
    """A lexical knowledge retrieval hit with a resolvable citation."""

    chunk_id: str
    version_id: str
    source_id: str
    source_name: str
    version_no: int
    ordinal: int
    kind: str
    header_path: tuple[str, ...]
    source_start: int
    source_end: int
    text: str
    score: float
    snippet: str


@dataclass(frozen=True)
class QueryResult:
    """Bounded result of a knowledge query."""

    query: str
    project_id: str
    hits: tuple[ChunkHit, ...] = field(default_factory=tuple)
    total_hits: int = 0
    degraded: bool = False
    elapsed_ms: int = 0
    gates: tuple[str, ...] = field(default_factory=tuple)
