"""SQLite repository for the knowledge truth store.

The knowledge database is a separate truth store from plastic_memory.db.
Every mutation runs inside a transaction and is recorded in
knowledge_audit_events; derived projections flow through the index outbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from plastic_promise.knowledge.contracts import (
    ActiveProjectCursor,
    ActiveProjectPage,
    JobView,
    SemanticChunkCursor,
    SourceView,
    VersionView,
    utc_now_iso,
)


def _require_project_scope(value: object) -> str:
    project_id = str(value or "").strip()
    if not project_id or project_id.casefold() in {"project:unknown", "unknown"}:
        raise ValueError("knowledge_project_scope_required")
    return project_id


def _require_project_owner(
    connection: sqlite3.Connection,
    *,
    query: str,
    object_id: object,
    project_id: str,
    error: str,
) -> None:
    row = connection.execute(query, (str(object_id or ""),)).fetchone()
    if row is None or str(row[0]) != project_id:
        raise ValueError(error)


SCHEMA_TABLES: tuple[str, ...] = (
    "knowledge_spaces",
    "knowledge_sources",
    "knowledge_source_versions",
    "knowledge_chunks",
    "knowledge_ingest_jobs",
    "knowledge_audit_events",
    "knowledge_index_outbox",
    "knowledge_generations",
    "knowledge_semantic_jobs",
    "knowledge_semantic_units",
    "knowledge_domains",
    "knowledge_claims",
    "knowledge_artifacts",
    "knowledge_citations",
)

_SCHEMA_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_spaces (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        retired_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_spaces_project
        ON knowledge_spaces(project_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_spaces_project_name
        ON knowledge_spaces(project_id, name)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_sources (
        id TEXT PRIMARY KEY,
        space_id TEXT NOT NULL REFERENCES knowledge_spaces(id),
        project_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        origin_ref TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        active_version_id TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_sources_project
        ON knowledge_sources(project_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_source_versions (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES knowledge_sources(id),
        version_no INTEGER NOT NULL,
        blob_sha256 TEXT NOT NULL,
        byte_size INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        parser_id TEXT NOT NULL,
        parse_schema TEXT NOT NULL,
        document_title TEXT NOT NULL DEFAULT '',
        structure_manifest_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        superseded_at TEXT,
        UNIQUE(source_id, version_no),
        UNIQUE(source_id, content_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_versions_source_status
        ON knowledge_source_versions(source_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL REFERENCES knowledge_source_versions(id),
        chunk_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        kind TEXT NOT NULL,
        header_path_json TEXT NOT NULL DEFAULT '[]',
        source_start INTEGER NOT NULL,
        source_end INTEGER NOT NULL,
        text_hash TEXT NOT NULL,
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        UNIQUE(version_id, chunk_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_version_status
        ON knowledge_chunks(version_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_ingest_jobs (
        id TEXT PRIMARY KEY,
        source_id TEXT,
        project_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_owner TEXT,
        lease_expires_at TEXT,
        error TEXT,
        result_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_jobs_project_status
        ON knowledge_ingest_jobs(project_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_audit_events (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        object_type TEXT NOT NULL,
        object_id TEXT,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_audit_project_time
        ON knowledge_audit_events(project_id, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_index_outbox (
        id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL,
        op TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TEXT NOT NULL,
        processed_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_outbox_status_time
        ON knowledge_index_outbox(status, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_generations (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        manifest_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        activated_at TEXT,
        retired_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_semantic_jobs (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        space_id TEXT,
        version_id TEXT,
        batch_sha256 TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        lease_owner TEXT,
        lease_expires_at TEXT,
        next_attempt_at TEXT,
        error_code TEXT,
        error_detail TEXT,
        batch_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_semantic_jobs_batch
        ON knowledge_semantic_jobs(project_id, batch_sha256)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_semantic_jobs_status
        ON knowledge_semantic_jobs(status, next_attempt_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_semantic_units (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES knowledge_semantic_jobs(id),
        project_id TEXT NOT NULL,
        space_id TEXT,
        source_id TEXT,
        version_id TEXT,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        text_hash TEXT NOT NULL,
        evidence_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        payload_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_semantic_units_project
        ON knowledge_semantic_units(project_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_domains (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'candidate',
        parent_domain_id TEXT,
        aliases_json TEXT NOT NULL DEFAULT '[]',
        lineage_json TEXT NOT NULL DEFAULT '[]',
        evidence_json TEXT NOT NULL DEFAULT '{}',
        source_count INTEGER NOT NULL DEFAULT 0,
        distinct_spaces INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        retired_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_domains_project_name
        ON knowledge_domains(project_id, name) WHERE retired_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_domains_project_kind
        ON knowledge_domains(project_id, kind)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_claims (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        claim_text TEXT NOT NULL,
        claim_hash TEXT NOT NULL,
        stance TEXT NOT NULL DEFAULT 'neutral',
        temporal_start TEXT,
        temporal_end TEXT,
        risk_tier TEXT NOT NULL DEFAULT 'low',
        status TEXT NOT NULL DEFAULT 'active',
        evidence_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
        source_id TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(project_id, claim_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_claims_project_status
        ON knowledge_claims(project_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_artifacts (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        risk_tier TEXT NOT NULL DEFAULT 'low',
        citation_coverage REAL NOT NULL DEFAULT 0.0,
        source_ids_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(project_id, content_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_artifacts_project_status
        ON knowledge_artifacts(project_id, status, kind)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_citations (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES knowledge_artifacts(id),
        chunk_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        citation_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(artifact_id, chunk_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_citations_project
        ON knowledge_citations(project_id, chunk_id)
    """,
)


def schema_ddl_statements() -> tuple[str, ...]:
    """Return the idempotent DDL statements for the knowledge schema."""
    return _SCHEMA_DDL


class KnowledgeRepository:
    """Owns all SQLite access for the knowledge truth store."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._read_only = read_only
        if not read_only:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def connect(self) -> sqlite3.Connection:
        if self._read_only:
            connection = sqlite3.connect(f"{self._db_path.as_uri()}?mode=ro", uri=True)
            connection.execute("PRAGMA query_only = ON")
        else:
            connection = sqlite3.connect(self._db_path)
            connection.execute("PRAGMA journal_mode = WAL")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def init_schema(self) -> list[str]:
        """Apply the idempotent schema and return applied table names."""
        applied: list[str] = []
        with self.connect() as connection:
            for statement in _SCHEMA_DDL:
                connection.execute(statement)
        for table in SCHEMA_TABLES:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
            if row is not None:
                applied.append(table)
        return applied

    def present_tables(self) -> dict[str, bool]:
        with self.connect() as connection:
            rows = {
                str(row["name"])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        return {table: table in rows for table in SCHEMA_TABLES}

    def quick_check(self) -> str:
        with self.connect() as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        return str(row[0] if row else "unknown")

    def integrity_check(self) -> str:
        with self.connect() as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0] if row else "unknown")

    # -- spaces ----------------------------------------------------------

    def get_or_create_space(self, project_id: str, name: str, description: str = "") -> str:
        project_id = _require_project_scope(project_id)
        normalized = (name or "default").strip() or "default"
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM knowledge_spaces WHERE project_id=? AND name=?",
                (project_id, normalized),
            ).fetchone()
            if row is not None:
                return str(row["id"])
            space_id = f"ksp_{uuid.uuid4().hex[:16]}"
            now = utc_now_iso()
            connection.execute(
                "INSERT INTO knowledge_spaces (id, project_id, name, description, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (space_id, project_id, normalized, description, now, now),
            )
            return space_id

    def space_exists(self, space_id: str, project_id: str) -> bool:
        project_id = _require_project_scope(project_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM knowledge_spaces WHERE id=? AND project_id=? AND retired_at IS NULL",
                (space_id, project_id),
            ).fetchone()
        return row is not None

    # -- sources ---------------------------------------------------------

    def create_source(
        self,
        *,
        project_id: str,
        space_id: str,
        kind: str,
        name: str,
        origin_ref: str | None,
    ) -> SourceView:
        project_id = _require_project_scope(project_id)
        source_id = f"ksrc_{uuid.uuid4().hex[:16]}"
        now = utc_now_iso()
        with self.connect() as connection:
            _require_project_owner(
                connection,
                query="SELECT project_id FROM knowledge_spaces WHERE id=? AND retired_at IS NULL",
                object_id=space_id,
                project_id=project_id,
                error="knowledge_space_project_mismatch",
            )
            connection.execute(
                "INSERT INTO knowledge_sources"
                " (id, space_id, project_id, kind, name, origin_ref, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (source_id, space_id, project_id, kind, name, origin_ref, now, now),
            )
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> SourceView:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_sources WHERE id=?", (source_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"knowledge source not found: {source_id}")
        return SourceView(
            id=str(row["id"]),
            space_id=str(row["space_id"]),
            project_id=str(row["project_id"]),
            kind=str(row["kind"]),
            name=str(row["name"]),
            origin_ref=row["origin_ref"],
            status=str(row["status"]),
            active_version_id=row["active_version_id"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list_sources(self, project_id: str, *, limit: int = 100) -> list[SourceView]:
        if not self._db_path.is_file():
            return []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_sources WHERE project_id=?"
                " ORDER BY updated_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [self._source_view(row) for row in rows]

    def list_active_project_page(
        self,
        *,
        limit: int = 100,
        after_cursor: ActiveProjectCursor | None = None,
    ) -> ActiveProjectPage:
        """Return one stable keyset page of projects owning active sources."""
        if not self._db_path.is_file():
            return ActiveProjectPage(project_ids=(), next_cursor=None, has_more=False)
        cursor_filter = ""
        params: list[Any] = []
        if after_cursor is not None:
            cursor_filter = " WHERE last_updated < ? OR (last_updated = ? AND project_id > ?)"
            params.extend(
                (
                    after_cursor.last_updated,
                    after_cursor.last_updated,
                    after_cursor.project_id,
                )
            )
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "WITH active_projects AS ("
                " SELECT project_id, MAX(updated_at) AS last_updated"
                " FROM knowledge_sources WHERE status='active' GROUP BY project_id"
                ") SELECT project_id, last_updated FROM active_projects"
                + cursor_filter
                + " ORDER BY last_updated DESC, project_id LIMIT ?",
                tuple(params),
            ).fetchall()
        next_cursor = None
        if rows:
            last = rows[-1]
            next_cursor = ActiveProjectCursor(
                last_updated=str(last["last_updated"]),
                project_id=str(last["project_id"]),
            )
        return ActiveProjectPage(
            project_ids=tuple(str(row["project_id"]) for row in rows),
            next_cursor=next_cursor,
            has_more=len(rows) == limit,
        )

    @staticmethod
    def _source_view(row: sqlite3.Row) -> SourceView:
        return SourceView(
            id=str(row["id"]),
            space_id=str(row["space_id"]),
            project_id=str(row["project_id"]),
            kind=str(row["kind"]),
            name=str(row["name"]),
            origin_ref=row["origin_ref"],
            status=str(row["status"]),
            active_version_id=row["active_version_id"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    # -- versions --------------------------------------------------------

    def find_version_by_content_hash(self, source_id: str, content_hash: str) -> VersionView | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_source_versions WHERE source_id=? AND content_hash=?",
                (source_id, content_hash),
            ).fetchone()
        return self._version_view(row) if row is not None else None

    def create_version(
        self,
        *,
        source_id: str,
        content_hash: str,
        blob_sha256: str,
        byte_size: int,
        parser_id: str,
        parse_schema: str,
        document_title: str,
        structure_manifest: dict[str, Any],
    ) -> VersionView:
        version_id = f"ksv_{uuid.uuid4().hex[:16]}"
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM knowledge_source_versions"
                " WHERE source_id=?",
                (source_id,),
            ).fetchone()
            version_no = int(row[0])
            connection.execute(
                "INSERT INTO knowledge_source_versions"
                " (id, source_id, version_no, blob_sha256, byte_size, content_hash,"
                "  parser_id, parse_schema, document_title, structure_manifest_json,"
                "  status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    version_id,
                    source_id,
                    version_no,
                    blob_sha256,
                    byte_size,
                    content_hash,
                    parser_id,
                    parse_schema,
                    document_title,
                    json.dumps(structure_manifest, ensure_ascii=False),
                    now,
                ),
            )
            connection.execute(
                "UPDATE knowledge_sources SET active_version_id=?, updated_at=? WHERE id=?",
                (version_id, now, source_id),
            )
        return self.get_version(version_id)

    def supersede_older_versions(self, source_id: str, keep_version_id: str) -> int:
        """Mark non-active versions stale when a new version is admitted."""
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE knowledge_source_versions SET status='stale', superseded_at=?"
                " WHERE source_id=? AND id<>? AND status='active'",
                (now, source_id, keep_version_id),
            )
            affected = cursor.rowcount
            if affected:
                connection.execute(
                    "UPDATE knowledge_chunks SET status='stale'"
                    " WHERE version_id IN ("
                    "  SELECT id FROM knowledge_source_versions"
                    "  WHERE source_id=? AND id<>? AND status='stale')",
                    (source_id, keep_version_id),
                )
        return affected

    def get_version(self, version_id: str) -> VersionView:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_source_versions WHERE id=?", (version_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"knowledge version not found: {version_id}")
        return self._version_view(row)

    def list_versions(self, source_id: str, *, limit: int = 50) -> list[VersionView]:
        if not self._db_path.is_file():
            return []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_source_versions WHERE source_id=?"
                " ORDER BY version_no DESC LIMIT ?",
                (source_id, limit),
            ).fetchall()
        return [self._version_view(row) for row in rows]

    def _version_view(self, row: sqlite3.Row) -> VersionView:
        with self.connect() as connection:
            count_row = connection.execute(
                "SELECT COUNT(*) AS n FROM knowledge_chunks WHERE version_id=?",
                (str(row["id"]),),
            ).fetchone()
        return VersionView(
            id=str(row["id"]),
            source_id=str(row["source_id"]),
            version_no=int(row["version_no"]),
            blob_sha256=str(row["blob_sha256"]),
            byte_size=int(row["byte_size"]),
            content_hash=str(row["content_hash"]),
            parser_id=str(row["parser_id"]),
            parse_schema=str(row["parse_schema"]),
            document_title=str(row["document_title"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            superseded_at=row["superseded_at"],
            chunk_count=int(count_row["n"]),
        )

    # -- chunks ----------------------------------------------------------

    def insert_chunks(self, version_id: str, chunks: list[dict[str, Any]]) -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            for chunk in chunks:
                connection.execute(
                    "INSERT INTO knowledge_chunks"
                    " (id, version_id, chunk_id, ordinal, kind, header_path_json,"
                    "  source_start, source_end, text_hash, text, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                    (
                        f"chk_{uuid.uuid4().hex[:16]}",
                        version_id,
                        str(chunk["chunk_id"]),
                        int(chunk["ordinal"]),
                        str(chunk["kind"]),
                        json.dumps(chunk.get("header_path", []), ensure_ascii=False),
                        int(chunk["source_start"]),
                        int(chunk["source_end"]),
                        str(chunk["text_hash"]),
                        str(chunk["text"]),
                        now,
                    ),
                )
        return len(chunks)

    def iter_searchable_chunks(
        self,
        project_id: str,
        *,
        space_id: str | None = None,
        include_stale: bool = False,
    ) -> list[sqlite3.Row]:
        """Return project-scoped chunk rows for lexical scoring.

        Only chunks belonging to the requested project are ever exposed;
        cross-project rows are excluded at the SQL boundary.
        """
        status_filter = "" if include_stale else " AND kc.status='active'"
        space_filter = ""
        params: list[Any] = [project_id]
        if space_id:
            space_filter = " AND ksrc.space_id=?"
            params.append(space_id)
        if not self._db_path.is_file():
            return []
        query = (
            "SELECT kc.id AS row_id, kc.chunk_id, kc.ordinal, kc.kind, kc.header_path_json,"
            "       kc.source_start, kc.source_end, kc.text_hash, kc.text,"
            "       kc.version_id, ksv.source_id, ksv.version_no, ksv.document_title,"
            "       ksrc.name AS source_name, ksv.status AS version_status, kc.status AS chunk_status"
            "  FROM knowledge_chunks kc"
            "  JOIN knowledge_source_versions ksv ON ksv.id = kc.version_id"
            "  JOIN knowledge_sources ksrc ON ksrc.id = ksv.source_id"
            "  JOIN knowledge_spaces ksp ON ksp.id = ksrc.space_id"
            " WHERE ksrc.project_id = ?"
            + space_filter
            + status_filter
            + " ORDER BY kc.created_at DESC"
        )
        with self.connect() as connection:
            return connection.execute(query, params).fetchall()

    def list_chunks(self, source_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return bounded chunk projections for one source, newest version first."""
        if not self._db_path.is_file():
            return []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT kc.chunk_id, kc.ordinal, kc.kind, kc.header_path_json,"
                "       kc.source_start, kc.source_end, kc.text,"
                "       kc.version_id, ksv.version_no, ksv.status AS version_status"
                "  FROM knowledge_chunks kc"
                "  JOIN knowledge_source_versions ksv ON ksv.id = kc.version_id"
                " WHERE ksv.source_id = ?"
                " ORDER BY ksv.version_no DESC, kc.ordinal ASC"
                " LIMIT ?",
                (source_id, limit),
            ).fetchall()
        return [
            {
                "chunk_id": str(row["chunk_id"]),
                "version_id": str(row["version_id"]),
                "version_no": int(row["version_no"]),
                "ordinal": int(row["ordinal"]),
                "kind": str(row["kind"]),
                "header_path": json.loads(str(row["header_path_json"])),
                "source_start": int(row["source_start"]),
                "source_end": int(row["source_end"]),
                "status": str(row["version_status"]),
                "snippet": " ".join(str(row["text"]).split())[:220],
            }
            for row in rows
        ]

    # -- jobs ------------------------------------------------------------

    def create_job(self, *, project_id: str, source_id: str | None, stage: str) -> JobView:
        project_id = _require_project_scope(project_id)
        job_id = f"kj_{uuid.uuid4().hex[:16]}"
        now = utc_now_iso()
        with self.connect() as connection:
            if source_id is not None:
                _require_project_owner(
                    connection,
                    query="SELECT project_id FROM knowledge_sources WHERE id=?",
                    object_id=source_id,
                    project_id=project_id,
                    error="knowledge_job_source_project_mismatch",
                )
            connection.execute(
                "INSERT INTO knowledge_ingest_jobs"
                " (id, source_id, project_id, stage, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (job_id, source_id, project_id, stage, now, now),
            )
        return self.get_job(job_id)

    def claim_job(self, job_id: str, owner: str, *, lease_seconds: int = 300) -> bool:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE knowledge_ingest_jobs"
                " SET status='running', attempts=attempts+1, lease_owner=?,"
                "     lease_expires_at=?, updated_at=?, error=NULL"
                " WHERE id=? AND (status='pending'"
                "   OR (status='running' AND lease_expires_at IS NOT NULL"
                "       AND lease_expires_at < ?))",
                (owner, _add_seconds(now, lease_seconds), now, job_id, now),
            )
            return cursor.rowcount == 1

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> JobView:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE knowledge_ingest_jobs"
                " SET status=?, result_json=?, error=?, updated_at=?, finished_at=?, lease_owner=NULL"
                " WHERE id=?",
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    error,
                    now,
                    now,
                    job_id,
                ),
            )
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> bool:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE knowledge_ingest_jobs"
                " SET status='cancelled', updated_at=?, finished_at=?"
                " WHERE id=? AND status='pending'",
                (now, now, job_id),
            )
            return cursor.rowcount == 1

    def get_job(self, job_id: str) -> JobView:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_ingest_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"knowledge job not found: {job_id}")
        return self._job_view(row)

    def list_jobs(
        self, project_id: str, *, status: str | None = None, limit: int = 100
    ) -> list[JobView]:
        if not self._db_path.is_file():
            return []
        params: list[Any] = [project_id]
        status_filter = ""
        if status:
            status_filter = " AND status=?"
            params.append(status)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_ingest_jobs WHERE project_id=?"
                + status_filter
                + " ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._job_view(row) for row in rows]

    @staticmethod
    def _job_view(row: sqlite3.Row) -> JobView:
        result = row["result_json"]
        return JobView(
            id=str(row["id"]),
            source_id=row["source_id"],
            project_id=str(row["project_id"]),
            stage=str(row["stage"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            error=row["error"],
            result_json=json.loads(result) if result else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            finished_at=row["finished_at"],
        )

    # -- semantic compilation ---------------------------------------------

    def list_active_chunks_for_semantic(
        self,
        project_id: str,
        *,
        limit: int = 500,
        after_cursor: SemanticChunkCursor | None = None,
    ) -> list[dict[str, Any]]:
        """Return one stable keyset page of active chunks for batch planning."""
        cursor_filter = ""
        params: list[Any] = [project_id]
        if after_cursor is not None:
            cursor_filter = (
                " AND (kc.created_at > ?"
                " OR (kc.created_at = ? AND kc.ordinal > ?)"
                " OR (kc.created_at = ? AND kc.ordinal = ? AND kc.id > ?))"
            )
            params.extend(
                (
                    after_cursor.created_at,
                    after_cursor.created_at,
                    after_cursor.ordinal,
                    after_cursor.created_at,
                    after_cursor.ordinal,
                    after_cursor.row_id,
                )
            )
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT kc.id AS row_id, kc.chunk_id, kc.ordinal, kc.kind,"
                "       kc.header_path_json, kc.text, kc.text_hash,"
                "       kc.created_at,"
                "       kc.version_id, ksv.source_id, ksrc.space_id, ksrc.project_id"
                "  FROM knowledge_chunks kc"
                "  JOIN knowledge_source_versions ksv ON ksv.id = kc.version_id"
                "  JOIN knowledge_sources ksrc ON ksrc.id = ksv.source_id"
                " WHERE ksrc.project_id = ? AND kc.status = 'active'"
                "   AND ksv.status = 'active' AND ksrc.status = 'active'"
                + cursor_filter
                + " ORDER BY kc.created_at, kc.ordinal, kc.id LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def semantic_job_by_batch(self, project_id: str, batch_sha256: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_semantic_jobs WHERE project_id=? AND batch_sha256=?",
                (project_id, batch_sha256),
            ).fetchone()
        return dict(row) if row is not None else None

    def create_semantic_job(self, payload: dict[str, Any]) -> str:
        project_id = _require_project_scope(payload.get("project_id"))
        job_id = f"ksj_{uuid.uuid4().hex[:16]}"
        now = utc_now_iso()
        with self.connect() as connection:
            space_id = payload.get("space_id")
            if space_id is not None:
                _require_project_owner(
                    connection,
                    query="SELECT project_id FROM knowledge_spaces WHERE id=? AND retired_at IS NULL",
                    object_id=space_id,
                    project_id=project_id,
                    error="knowledge_semantic_space_project_mismatch",
                )
            version_id = payload.get("version_id")
            if version_id is not None:
                _require_project_owner(
                    connection,
                    query=(
                        "SELECT ksrc.project_id FROM knowledge_source_versions ksv "
                        "JOIN knowledge_sources ksrc ON ksrc.id=ksv.source_id WHERE ksv.id=?"
                    ),
                    object_id=version_id,
                    project_id=project_id,
                    error="knowledge_semantic_version_project_mismatch",
                )
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_semantic_jobs"
                " (id, project_id, space_id, version_id, batch_sha256, status,"
                "  max_attempts, batch_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    job_id,
                    project_id,
                    payload.get("space_id"),
                    payload.get("version_id"),
                    payload["batch_sha256"],
                    payload.get("max_attempts", 5),
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return job_id

    def claim_ready_semantic_jobs(
        self,
        owner: str,
        *,
        limit: int = 10,
        lease_seconds: int = 300,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        now = utc_now_iso()
        lease_expiry = _add_seconds(now, lease_seconds)
        project_filter = ""
        params: list[Any] = [now]
        if project_id is not None:
            project_filter = " AND project_id=?"
            params.append(project_id)
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_semantic_jobs"
                " WHERE status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                + project_filter
                + " ORDER BY created_at LIMIT ?",
                tuple(params),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                updated = connection.execute(
                    "UPDATE knowledge_semantic_jobs"
                    " SET status='building', lease_owner=?, lease_expires_at=?, updated_at=?"
                    " WHERE id=? AND status='pending'",
                    (owner, lease_expiry, now, str(row["id"])),
                )
                if updated.rowcount == 1:
                    claimed.append(dict(row))
        return claimed

    def renew_semantic_job_lease(
        self,
        job_id: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> bool:
        """Extend an unexpired lease only for its current unique owner."""
        now = utc_now_iso()
        lease_expiry = _add_seconds(now, lease_seconds)
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE knowledge_semantic_jobs SET lease_expires_at=?, updated_at=?"
                " WHERE id=? AND status='building' AND lease_owner=?"
                " AND lease_expires_at IS NOT NULL AND lease_expires_at >= ?",
                (lease_expiry, now, job_id, owner, now),
            )
        return updated.rowcount == 1

    def complete_semantic_job(self, job_id: str, *, owner: str) -> bool:
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE knowledge_semantic_jobs"
                " SET status='done', lease_owner=NULL, lease_expires_at=NULL,"
                "     error_code=NULL, error_detail=NULL, finished_at=?, updated_at=?"
                " WHERE id=? AND status='building' AND lease_owner=?",
                (utc_now_iso(), utc_now_iso(), job_id, owner),
            )
        return updated.rowcount == 1

    def fail_semantic_job(
        self,
        job_id: str,
        error_code: str,
        error_detail: str,
        *,
        retryable: bool,
        owner: str,
    ) -> bool:
        now = utc_now_iso()
        try:
            backoff_base = max(
                0, int(os.environ.get("PP_KNOWLEDGE_SEMANTIC_BACKOFF_BASE_SECONDS", "15"))
            )
        except (TypeError, ValueError):
            backoff_base = 15
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempts, max_attempts FROM knowledge_semantic_jobs"
                " WHERE id=? AND status='building' AND lease_owner=?",
                (job_id, owner),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"]) + 1
            max_attempts = int(row["max_attempts"])
            if retryable and attempts < max_attempts:
                backoff_seconds = min(3600, 2 ** (attempts - 1) * backoff_base)
                next_attempt = _add_seconds(now, backoff_seconds)
                status = "pending"
            else:
                status = "failed"
                next_attempt = None
            updated = connection.execute(
                "UPDATE knowledge_semantic_jobs"
                " SET status=?, attempts=?, lease_owner=NULL, lease_expires_at=NULL,"
                "     next_attempt_at=?, error_code=?, error_detail=?, finished_at=?, updated_at=?"
                " WHERE id=? AND status='building' AND lease_owner=?",
                (
                    status,
                    attempts,
                    next_attempt,
                    error_code,
                    error_detail[:2000],
                    now if status == "failed" else None,
                    now,
                    job_id,
                    owner,
                ),
            )
        return updated.rowcount == 1

    def reconcile_semantic_jobs(self, now_iso: str | None = None) -> int:
        """Reclaim expired leases so crashed workers do not strand batches."""
        now = now_iso or utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE knowledge_semantic_jobs"
                " SET status='pending', lease_owner=NULL, lease_expires_at=NULL,"
                "     error_code='lease_expired', updated_at=?"
                " WHERE status='building' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
                (now, now),
            )
        return int(cursor.rowcount)

    def insert_semantic_units(self, units: list[dict[str, Any]]) -> int:
        inserted = 0
        with self.connect() as connection:
            for unit in units:
                project_id = _require_project_scope(unit.get("project_id"))
                _require_project_owner(
                    connection,
                    query="SELECT project_id FROM knowledge_semantic_jobs WHERE id=?",
                    object_id=unit.get("job_id"),
                    project_id=project_id,
                    error="knowledge_semantic_unit_job_project_mismatch",
                )
                ownership_queries = (
                    (
                        "space_id",
                        "SELECT project_id FROM knowledge_spaces WHERE id=? AND retired_at IS NULL",
                        "knowledge_semantic_unit_space_project_mismatch",
                    ),
                    (
                        "source_id",
                        "SELECT project_id FROM knowledge_sources WHERE id=?",
                        "knowledge_semantic_unit_source_project_mismatch",
                    ),
                    (
                        "version_id",
                        "SELECT ksrc.project_id FROM knowledge_source_versions ksv "
                        "JOIN knowledge_sources ksrc ON ksrc.id=ksv.source_id WHERE ksv.id=?",
                        "knowledge_semantic_unit_version_project_mismatch",
                    ),
                )
                for field, query, error in ownership_queries:
                    object_id = unit.get(field)
                    if object_id is not None:
                        _require_project_owner(
                            connection,
                            query=query,
                            object_id=object_id,
                            project_id=project_id,
                            error=error,
                        )
                row = connection.execute(
                    "INSERT OR IGNORE INTO knowledge_semantic_units"
                    " (id, job_id, project_id, space_id, source_id, version_id, kind, text,"
                    "  text_hash, evidence_chunk_ids_json, metadata_json, payload_hash,"
                    "  status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                    (
                        f"ksu_{uuid.uuid4().hex[:16]}",
                        unit["job_id"],
                        project_id,
                        unit.get("space_id"),
                        unit.get("source_id"),
                        unit.get("version_id"),
                        unit["kind"],
                        unit["text"],
                        unit["text_hash"],
                        json.dumps(unit.get("evidence_chunk_ids", []), ensure_ascii=False),
                        json.dumps(unit.get("metadata", {}), ensure_ascii=False),
                        unit["payload_hash"],
                        utc_now_iso(),
                    ),
                )
                inserted += int(row.rowcount)
        return inserted

    def semantic_units_for_project(
        self, project_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_semantic_units WHERE project_id=?"
                " AND status='active' ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def semantic_status(self, project_id: str) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, count(*) AS n FROM knowledge_semantic_jobs"
                " WHERE project_id=? GROUP BY status",
                (project_id,),
            ).fetchall()
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        return {
            "pending": counts.get("pending", 0),
            "building": counts.get("building", 0),
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
        }

    # -- derived knowledge generations ----------------------------------

    def shadow_generation(self, generation_id: str) -> dict[str, Any] | None:
        """Return one rebuildable generation record without selecting it for reads."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_generations WHERE id=?", (generation_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def record_shadow_generation(
        self,
        generation_id: str,
        status: str,
        manifest: dict[str, Any],
        *,
        actor: str,
    ) -> None:
        """Persist a shadow transition and its audit event in one transaction."""
        if status not in {"building", "shadow", "failed"}:
            raise ValueError("knowledge_shadow_generation_status_invalid")
        serialized = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        if len(serialized.encode("utf-8")) > 64 * 1024:
            raise ValueError("knowledge_shadow_generation_manifest_oversized")
        identity = manifest.get("identity")
        if not isinstance(identity, dict):
            raise ValueError("knowledge_shadow_generation_identity_invalid")
        project_id = str(identity.get("project_id") or "").strip()
        normalized_actor = str(actor or "").strip()
        if not project_id or not normalized_actor:
            raise ValueError("knowledge_shadow_generation_audit_identity_invalid")
        identity_payload = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        identity_sha256 = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
        now = utc_now_iso()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT status FROM knowledge_generations WHERE id=?", (generation_id,)
            ).fetchone()
            if existing is not None and str(existing["status"]) not in {
                "building",
                "shadow",
                "failed",
            }:
                raise ValueError("knowledge_shadow_generation_not_owned")
            connection.execute(
                "INSERT INTO knowledge_generations"
                " (id, status, manifest_json, created_at, activated_at, retired_at)"
                " VALUES (?, ?, ?, ?, NULL, NULL)"
                " ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
                " manifest_json=excluded.manifest_json",
                (generation_id, status, serialized, now),
            )
            previous_status = str(existing["status"]) if existing is not None else None
            if previous_status != status:
                detail: dict[str, Any] = {
                    "from_status": previous_status,
                    "to_status": status,
                    "identity_sha256": identity_sha256,
                    "schema_version": str(manifest.get("schema_version") or "")[:128],
                    "canonical_record_count": int(manifest.get("canonical_record_count") or 0),
                    "promotion_eligible": False,
                }
                failure_code = str(manifest.get("failure_code") or "")
                if failure_code:
                    detail["failure_code"] = failure_code[:128]
                connection.execute(
                    "INSERT INTO knowledge_audit_events"
                    " (id, project_id, actor, action, object_type, object_id,"
                    "  detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"ka_{uuid.uuid4().hex[:16]}",
                        project_id,
                        normalized_actor,
                        "generation_status_changed",
                        "knowledge_generation",
                        generation_id,
                        json.dumps(detail, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )

    def upsert_domain_candidate(
        self,
        *,
        project_id: str,
        name: str,
        description: str,
        source_id: str,
        space_id: str | None,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        project_id = _require_project_scope(project_id)
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_domains WHERE project_id=? AND name=?",
                (project_id, name),
            ).fetchone()
            if row is None:
                domain_id = f"kdom_{uuid.uuid4().hex[:16]}"
                connection.execute(
                    "INSERT INTO knowledge_domains"
                    " (id, project_id, name, description, kind, aliases_json, lineage_json,"
                    "  evidence_json, source_count, distinct_spaces, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'candidate', '[]', ?, ?, 0, 0, ?, ?)",
                    (
                        domain_id,
                        project_id,
                        name,
                        description[:2000],
                        json.dumps(
                            [{"event": "candidate_created", "at": now, "source_id": source_id}],
                            ensure_ascii=False,
                        ),
                        json.dumps(evidence, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            else:
                domain_id = str(row["id"])
                current_evidence = json.loads(str(row["evidence_json"]))
                seen_sources = set(str(current_evidence.get("source_ids") or "").split(","))
                seen_spaces = set(str(current_evidence.get("space_ids") or "").split(","))
                if source_id:
                    seen_sources.add(source_id)
                if space_id:
                    seen_spaces.add(space_id)
                connection.execute(
                    "UPDATE knowledge_domains"
                    " SET description=?, evidence_json=?, source_count=?, distinct_spaces=?,"
                    "     updated_at=?"
                    " WHERE id=?",
                    (
                        description[:2000] or str(row["description"]),
                        json.dumps(
                            {
                                "source_ids": ",".join(sorted(x for x in seen_sources if x)),
                                "space_ids": ",".join(sorted(x for x in seen_spaces if x)),
                            },
                            ensure_ascii=False,
                        ),
                        len([x for x in seen_sources if x]),
                        len([x for x in seen_spaces if x]),
                        now,
                        domain_id,
                    ),
                )
        return self._domain_by_id(domain_id)

    def _domain_by_id(self, domain_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_domains WHERE id=?", (domain_id,)
            ).fetchone()
        return dict(row) if row is not None else {}

    def list_domains(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_domains WHERE project_id=?"
                " ORDER BY kind, source_count DESC, name",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_domain_kind(
        self, domain_id: str, kind: str, *, event: str, detail: dict[str, Any]
    ) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT lineage_json, retired_at FROM knowledge_domains WHERE id=?",
                (domain_id,),
            ).fetchone()
            lineage = json.loads(str(row["lineage_json"])) if row is not None else []
            lineage.append({"event": event, "at": now, **detail})
            connection.execute(
                "UPDATE knowledge_domains SET kind=?, lineage_json=?, updated_at=?,"
                " retired_at=CASE WHEN ? THEN ? ELSE retired_at END WHERE id=?",
                (
                    kind,
                    json.dumps(lineage, ensure_ascii=False),
                    now,
                    kind in {"merged", "retired"},
                    now if kind in {"merged", "retired"} else None,
                    domain_id,
                ),
            )

    def create_domain(
        self,
        *,
        project_id: str,
        name: str,
        description: str,
        kind: str = "candidate",
        parent_domain_id: str | None = None,
    ) -> str:
        project_id = _require_project_scope(project_id)
        domain_id = f"kdom_{uuid.uuid4().hex[:16]}"
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_domains"
                " (id, project_id, name, description, kind, parent_domain_id,"
                "  aliases_json, lineage_json, evidence_json, source_count,"
                "  distinct_spaces, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, '[]', ?, '{}', 0, 0, ?, ?)",
                (
                    domain_id,
                    project_id,
                    name,
                    description[:2000],
                    kind,
                    parent_domain_id,
                    json.dumps(
                        [{"event": "domain_created", "at": now, "kind": kind}],
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                ),
            )
        return domain_id

    def append_domain_aliases(self, domain_id: str, aliases: list[str]) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT aliases_json FROM knowledge_domains WHERE id=?", (domain_id,)
            ).fetchone()
            current = json.loads(str(row["aliases_json"])) if row is not None else []
            merged = list(dict.fromkeys([*current, *aliases]))
            connection.execute(
                "UPDATE knowledge_domains SET aliases_json=?, updated_at=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), now, domain_id),
            )

    def insert_claim(self, claim: dict[str, Any]) -> str | None:
        project_id = _require_project_scope(claim.get("project_id"))
        claim_id = f"kcl_{uuid.uuid4().hex[:16]}"
        with self.connect() as connection:
            row = connection.execute(
                "INSERT OR IGNORE INTO knowledge_claims"
                " (id, project_id, claim_text, claim_hash, stance, temporal_start,"
                "  temporal_end, risk_tier, status, evidence_chunk_ids_json, source_id,"
                "  created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    claim_id,
                    project_id,
                    claim["claim_text"],
                    claim["claim_hash"],
                    claim["stance"],
                    claim.get("temporal_start"),
                    claim.get("temporal_end"),
                    claim["risk_tier"],
                    json.dumps(claim.get("evidence_chunk_ids", []), ensure_ascii=False),
                    claim.get("source_id"),
                    utc_now_iso(),
                ),
            )
        return claim_id if row.rowcount == 1 else None

    def list_claims(self, project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_claims WHERE project_id=?"
                " ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_artifact(self, artifact: dict[str, Any]) -> str:
        project_id = _require_project_scope(artifact.get("project_id"))
        artifact_id = f"kart_{uuid.uuid4().hex[:16]}"
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM knowledge_artifacts WHERE project_id=? AND content_hash=?",
                (project_id, artifact["content_hash"]),
            ).fetchone()
            if row is not None:
                return str(row["id"])
            connection.execute(
                "INSERT INTO knowledge_artifacts"
                " (id, project_id, kind, title, content, content_hash, status, risk_tier,"
                "  citation_coverage, source_ids_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, 0.0, ?, ?, ?)",
                (
                    artifact_id,
                    project_id,
                    artifact["kind"],
                    artifact["title"],
                    artifact["content"],
                    artifact["content_hash"],
                    artifact.get("risk_tier", "low"),
                    json.dumps(artifact.get("source_ids", []), ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return artifact_id

    def update_artifact_status(
        self, artifact_id: str, status: str, *, citation_coverage: float | None = None
    ) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE knowledge_artifacts SET status=?, updated_at=?,"
                " citation_coverage=CASE WHEN ? IS NULL THEN citation_coverage ELSE ? END"
                " WHERE id=?",
                (status, now, citation_coverage, citation_coverage, artifact_id),
            )

    def insert_citation(self, artifact_id: str, chunk_id: str, project_id: str) -> None:
        project_id = _require_project_scope(project_id)
        with self.connect() as connection:
            _require_project_owner(
                connection,
                query="SELECT project_id FROM knowledge_artifacts WHERE id=?",
                object_id=artifact_id,
                project_id=project_id,
                error="knowledge_citation_artifact_project_mismatch",
            )
            chunk = connection.execute(
                "SELECT 1 FROM knowledge_chunks kc "
                "JOIN knowledge_source_versions ksv ON ksv.id=kc.version_id "
                "JOIN knowledge_sources ksrc ON ksrc.id=ksv.source_id "
                "WHERE kc.chunk_id=? AND ksrc.project_id=?",
                (chunk_id, project_id),
            ).fetchone()
            if chunk is None:
                raise ValueError("knowledge_citation_chunk_project_mismatch")
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_citations"
                " (id, artifact_id, chunk_id, project_id, citation_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"kct_{uuid.uuid4().hex[:16]}",
                    artifact_id,
                    chunk_id,
                    project_id,
                    f"{artifact_id}:{chunk_id}",
                    utc_now_iso(),
                ),
            )

    def artifact_citation_coverage(self, artifact_id: str) -> float:
        with self.connect() as connection:
            total = connection.execute(
                "SELECT count(*) AS n FROM knowledge_citations WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()["n"]
            if int(total) == 0:
                return 0.0
            valid = connection.execute(
                "SELECT count(*) AS n FROM knowledge_citations kc"
                " JOIN knowledge_chunks ch ON ch.chunk_id = kc.chunk_id AND ch.status='active'"
                " WHERE kc.artifact_id=?",
                (artifact_id,),
            ).fetchone()["n"]
        return round(int(valid) / int(total), 4)

    def list_artifacts(self, project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_artifacts WHERE project_id=?"
                " ORDER BY status, created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def artifact_by_id(self, artifact_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    # -- audit and outbox -------------------------------------------------

    def audit(
        self,
        *,
        project_id: str,
        actor: str,
        action: str,
        object_type: str,
        object_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        project_id = _require_project_scope(project_id)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO knowledge_audit_events"
                " (id, project_id, actor, action, object_type, object_id, detail_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"ka_{uuid.uuid4().hex[:16]}",
                    project_id,
                    actor,
                    action,
                    object_type,
                    object_id,
                    json.dumps(detail or {}, ensure_ascii=False),
                    utc_now_iso(),
                ),
            )

    def enqueue_index(self, version_id: str, *, op: str = "index") -> str:
        outbox_id = f"kob_{uuid.uuid4().hex[:16]}"
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO knowledge_index_outbox"
                " (id, version_id, op, status, created_at)"
                " VALUES (?, ?, ?, 'pending', ?)",
                (outbox_id, version_id, op, utc_now_iso()),
            )
        return outbox_id

    def pending_index_entries(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_index_outbox WHERE status='pending'"
                " ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def audit_events(self, project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_audit_events WHERE project_id=?"
                " ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]


def _add_seconds(iso_timestamp: str, seconds: int) -> str:
    """Add seconds to an ISO-8601 UTC timestamp without external deps."""
    from datetime import datetime, timedelta, timezone

    parsed = datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    shifted = parsed + timedelta(seconds=seconds)
    return shifted.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-3] + "Z"
    parsed = datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    shifted = parsed.fromtimestamp(
        parsed.timestamp() + seconds, tz=parsed.tzinfo or __import__("datetime").timezone.utc
    )
    return shifted.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-3] + "Z"
