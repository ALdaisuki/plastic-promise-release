"""Project-scoped Markdown knowledge source storage.

The knowledge layer keeps the verbatim document as the source of truth and
stores structure-aware chunks as rebuildable derived material.  It intentionally
does not create canonical memories or fetch the internet; source discovery and
promotion are separate governed steps.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from plastic_promise.core.chunking import build_chunk_manifest, chunk_manifest_hash

KNOWLEDGE_SCHEMA = "plastic-promise/knowledge-markdown/v1"
ALLOWED_SOURCE_PLATFORMS = frozenset({"github", "xda", "hackernews", "juejin"})
DOCUMENT_STATUSES = frozenset({"active", "stale", "conflict", "superseded"})
_SLUG_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GOVERNANCE_RELATION_RE = re.compile(r"^(memory|knowledge):[A-Za-z0-9._:-]{1,200}$")


def _text(value: object) -> str:
    return str(value or "").strip()


def _scope_required(value: object) -> str:
    project = _text(value)
    if not project or project == "project:unknown":
        raise ValueError("knowledge_project_scope_required")
    return project


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else _json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value.casefold()).strip("-")[:48] or "knowledge"


def _document_id(project_id: str, source_uri: str) -> str:
    return "knowledge-doc:" + _sha256({"project_id": project_id, "source_uri": source_uri})[:32]


def _domain_id(project_id: str, name: str) -> str:
    return "knowledge-domain:" + _sha256({"project_id": project_id, "name": name})[:32]


def _chunk_id(document_id: str, ordinal: int, text_hash: str) -> str:
    return (
        "knowledge-chunk:"
        + _sha256({"document_id": document_id, "ordinal": ordinal, "text_hash": text_hash})[:32]
    )


def _title(markdown: str, source_uri: str) -> str:
    for line in markdown.splitlines():
        match = re.match(r"^ {0,3}#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()[:256]
    parsed = urlparse(source_uri)
    path_name = parsed.path.rstrip("/").split("/")[-1]
    return (path_name.rsplit(".", 1)[0] if "." in path_name else path_name)[:256] or "Untitled"


@dataclass(frozen=True)
class KnowledgeDomain:
    domain_id: str
    project_id: str
    name: str
    name_hash: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    project_id: str
    source_uri: str
    source_revision: str
    title: str
    visibility: str
    domain_id: str
    status: str
    raw_text: str
    source_sha256: str
    raw_text_sha256: str
    chunk_manifest_hash: str
    chunk_count: int
    created_at: str
    updated_at: str
    governance_reason: str = ""
    evidence_relation: str = ""
    governance_decision: str = ""


class KnowledgeDocumentStore:
    """Legacy Markdown test adapter; production uses ``KnowledgeRepository``.

    This module is retained only for compatibility with the original Markdown
    prototype.  MCP, HTTP, workers, and LanceDB shadow builders must use the
    canonical ``plastic_promise.knowledge.repository.KnowledgeRepository``
    truth store instead of opening these tables.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        allow_legacy_test_adapter: bool = False,
    ) -> None:
        if allow_legacy_test_adapter is not True:
            raise RuntimeError("legacy_knowledge_document_store_disabled")
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_domains (
                domain_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                name_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, name),
                CHECK(project_id != 'project:unknown')
            );
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                document_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                source_revision TEXT NOT NULL,
                title TEXT NOT NULL,
                visibility TEXT NOT NULL DEFAULT 'project',
                domain_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                raw_text TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                raw_text_sha256 TEXT NOT NULL,
                chunk_manifest_json TEXT NOT NULL,
                chunk_manifest_hash TEXT NOT NULL,
                governance_reason TEXT NOT NULL DEFAULT '',
                evidence_relation TEXT NOT NULL DEFAULT '',
                governance_decision TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, source_uri),
                CHECK(project_id != 'project:unknown'),
                CHECK(visibility IN ('project', 'shared', 'global')),
                CHECK(status IN ('active', 'stale', 'conflict', 'superseded'))
            );
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                kind TEXT NOT NULL,
                heading_path_json TEXT NOT NULL,
                source_start INTEGER NOT NULL,
                source_end INTEGER NOT NULL,
                text TEXT NOT NULL,
                text_sha256 TEXT NOT NULL,
                chunk_manifest_hash TEXT NOT NULL,
                embedding_status TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(document_id, ordinal),
                FOREIGN KEY(document_id) REFERENCES knowledge_documents(document_id),
                CHECK(project_id != 'project:unknown')
            );
            CREATE TABLE IF NOT EXISTS knowledge_source_registry (
                source_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_seen_revision TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, platform, source_uri),
                CHECK(project_id != 'project:unknown')
            );
            CREATE TABLE IF NOT EXISTS knowledge_governance_evidence (
                relation_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                CHECK(project_id != 'project:unknown'),
                CHECK(decision = 'approved'),
                CHECK(status = 'active')
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_documents_scope
            ON knowledge_documents(project_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_scope
            ON knowledge_chunks(project_id, document_id, ordinal);
            """
        )
        columns = {
            str(row[1])
            for row in self.conn.execute("PRAGMA table_info(knowledge_documents)").fetchall()
        }
        for name in ("governance_reason", "evidence_relation", "governance_decision"):
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE knowledge_documents ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                )
        self.conn.commit()

    def _require_governance_evidence(
        self, *, project_id: str, relation_id: str, decision: str
    ) -> None:
        relation = _text(relation_id)
        if _GOVERNANCE_RELATION_RE.fullmatch(relation) is None or _text(decision) != "approved":
            raise ValueError("knowledge_visibility_governance_invalid")
        row = self.conn.execute(
            "SELECT project_id, decision, status FROM knowledge_governance_evidence "
            "WHERE relation_id = ?",
            (relation,),
        ).fetchone()
        if (
            row is None
            or str(row[0]) != project_id
            or str(row[1]) != "approved"
            or str(row[2]) != "active"
        ):
            raise ValueError("knowledge_visibility_governance_evidence_unverified")

    def upsert_markdown(
        self,
        *,
        project_id: str,
        source_uri: str,
        markdown: str,
        source_revision: str = "",
        title: str = "",
        domain_name: str = "",
        visibility: str = "project",
        governance_reason: str = "",
        evidence_relation: str = "",
        governance_decision: str = "",
        target_chars: int = 1200,
        hard_chars: int = 1800,
    ) -> KnowledgeDocument:
        project = _scope_required(project_id)
        uri = _text(source_uri)
        if not uri or len(uri) > 2048:
            raise ValueError("knowledge_source_uri_required")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("knowledge_markdown_required")
        if visibility not in {"project", "shared", "global"}:
            raise ValueError("knowledge_visibility_invalid")
        governance = {
            "reason": _text(governance_reason),
            "evidence": _text(evidence_relation),
            "decision": _text(governance_decision),
        }
        if visibility in {"shared", "global"} and not all(governance.values()):
            raise ValueError("knowledge_visibility_governance_required")
        if visibility in {"shared", "global"}:
            self._require_governance_evidence(
                project_id=project,
                relation_id=governance["evidence"],
                decision=governance["decision"],
            )
        revision = _text(source_revision) or _sha256(markdown)
        if len(revision) > 256:
            raise ValueError("knowledge_source_revision_invalid")
        source_hash = _sha256(uri)
        raw_hash = _sha256(markdown)
        resolved_title = _text(title)[:256] or _title(markdown, uri)
        resolved_domain = _text(domain_name) or f"{_slug(resolved_title)}-{raw_hash[:8]}"
        manifest = build_chunk_manifest(
            markdown,
            target_chars=max(1, int(target_chars)),
            hard_chars=max(1, int(hard_chars)),
        )
        manifest_hash = chunk_manifest_hash(manifest)
        document_id = _document_id(project, uri)
        now = _utc_now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            domain = self._ensure_domain(project, resolved_domain)
            existing = self.conn.execute(
                "SELECT created_at FROM knowledge_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            created_at = str(existing[0]) if existing is not None else now
            self.conn.execute(
                """
                INSERT INTO knowledge_documents (
                    document_id, project_id, source_uri, source_revision, title, visibility,
                    domain_id, status, raw_text, source_sha256, raw_text_sha256,
                    chunk_manifest_json, chunk_manifest_hash, governance_reason,
                    evidence_relation, governance_decision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    source_revision = excluded.source_revision,
                    title = excluded.title,
                    visibility = excluded.visibility,
                    domain_id = excluded.domain_id,
                    status = 'active',
                    raw_text = excluded.raw_text,
                    source_sha256 = excluded.source_sha256,
                    raw_text_sha256 = excluded.raw_text_sha256,
                    chunk_manifest_json = excluded.chunk_manifest_json,
                    chunk_manifest_hash = excluded.chunk_manifest_hash,
                    governance_reason = excluded.governance_reason,
                    evidence_relation = excluded.evidence_relation,
                    governance_decision = excluded.governance_decision,
                    updated_at = excluded.updated_at
                """,
                (
                    document_id,
                    project,
                    uri,
                    revision,
                    resolved_title,
                    visibility,
                    domain.domain_id,
                    markdown,
                    source_hash,
                    raw_hash,
                    _json(manifest),
                    manifest_hash,
                    governance["reason"],
                    governance["evidence"],
                    governance["decision"],
                    created_at,
                    now,
                ),
            )
            self.conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
            for chunk in manifest["chunks"]:
                if not isinstance(chunk, dict):
                    raise ValueError("knowledge_chunk_manifest_invalid")
                text = str(chunk["text"])
                text_hash = str(chunk["text_hash"])
                self.conn.execute(
                    """
                    INSERT INTO knowledge_chunks (
                        chunk_id, document_id, project_id, ordinal, kind, heading_path_json,
                        source_start, source_end, text, text_sha256, chunk_manifest_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _chunk_id(document_id, int(chunk["ordinal"]), text_hash),
                        document_id,
                        project,
                        int(chunk["ordinal"]),
                        str(chunk["kind"]),
                        _json(chunk["header_path"]),
                        int(chunk["source_start"]),
                        int(chunk["source_end"]),
                        text,
                        text_hash,
                        manifest_hash,
                    ),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_document(document_id)

    def register_source(
        self,
        *,
        project_id: str,
        platform: str,
        source_uri: str,
    ) -> dict[str, Any]:
        project = _scope_required(project_id)
        normalized_platform = _text(platform).casefold()
        if normalized_platform not in ALLOWED_SOURCE_PLATFORMS:
            raise ValueError("knowledge_source_platform_not_allowed")
        uri = _text(source_uri)
        parsed = urlparse(uri)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("knowledge_source_uri_invalid")
        source_id = (
            "knowledge-source:"
            + _sha256({"project_id": project, "platform": normalized_platform, "source_uri": uri})[
                :32
            ]
        )
        now = _utc_now()
        self.conn.execute(
            """
            INSERT INTO knowledge_source_registry (
                source_id, project_id, platform, source_uri, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (source_id, project, normalized_platform, uri, now, now),
        )
        self.conn.commit()
        return {
            "source_id": source_id,
            "project_id": project,
            "platform": normalized_platform,
            "source_uri": uri,
            "enabled": True,
        }

    def get_document(self, document_id: str) -> KnowledgeDocument:
        row = self.conn.execute(
            "SELECT * FROM knowledge_documents WHERE document_id = ?",
            (_text(document_id),),
        ).fetchone()
        if row is None:
            raise ValueError("knowledge_document_not_found")
        return KnowledgeDocument(
            document_id=str(row["document_id"]),
            project_id=str(row["project_id"]),
            source_uri=str(row["source_uri"]),
            source_revision=str(row["source_revision"]),
            title=str(row["title"]),
            visibility=str(row["visibility"]),
            domain_id=str(row["domain_id"]),
            status=str(row["status"]),
            raw_text=str(row["raw_text"]),
            source_sha256=str(row["source_sha256"]),
            raw_text_sha256=str(row["raw_text_sha256"]),
            chunk_manifest_hash=str(row["chunk_manifest_hash"]),
            chunk_count=int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM knowledge_chunks WHERE document_id = ?",
                    (_text(document_id),),
                ).fetchone()[0]
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            governance_reason=str(row["governance_reason"] or ""),
            evidence_relation=str(row["evidence_relation"] or ""),
            governance_decision=str(row["governance_decision"] or ""),
        )

    def list_documents(self, *, project_id: str) -> tuple[KnowledgeDocument, ...]:
        project = _scope_required(project_id)
        rows = self.conn.execute(
            "SELECT document_id FROM knowledge_documents WHERE project_id = ? "
            "ORDER BY updated_at, document_id",
            (project,),
        ).fetchall()
        return tuple(self.get_document(str(row[0])) for row in rows)

    def list_chunks(self, *, project_id: str, document_id: str) -> tuple[dict[str, Any], ...]:
        project = _scope_required(project_id)
        rows = self.conn.execute(
            "SELECT * FROM knowledge_chunks WHERE project_id = ? AND document_id = ? "
            "ORDER BY ordinal",
            (project, _text(document_id)),
        ).fetchall()
        return tuple(
            {
                "chunk_id": str(row["chunk_id"]),
                "document_id": str(row["document_id"]),
                "project_id": str(row["project_id"]),
                "ordinal": int(row["ordinal"]),
                "kind": str(row["kind"]),
                "heading_path": json.loads(str(row["heading_path_json"])),
                "source_start": int(row["source_start"]),
                "source_end": int(row["source_end"]),
                "text": str(row["text"]),
                "text_sha256": str(row["text_sha256"]),
                "chunk_manifest_hash": str(row["chunk_manifest_hash"]),
                "embedding_status": str(row["embedding_status"]),
            }
            for row in rows
        )

    def _ensure_domain(self, project_id: str, name: str) -> KnowledgeDomain:
        domain_name = _text(name)[:128]
        if not domain_name:
            raise ValueError("knowledge_domain_name_required")
        domain_id = _domain_id(project_id, domain_name)
        now = _utc_now()
        name_hash = _sha256(domain_name)
        self.conn.execute(
            """
            INSERT INTO knowledge_domains (
                domain_id, project_id, name, name_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (domain_id, project_id, domain_name, name_hash, now, now),
        )
        return KnowledgeDomain(
            domain_id=domain_id,
            project_id=project_id,
            name=domain_name,
            name_hash=name_hash,
            status="active",
            created_at=now,
            updated_at=now,
        )


__all__ = [
    "ALLOWED_SOURCE_PLATFORMS",
    "KnowledgeDocument",
    "KnowledgeDocumentStore",
    "KnowledgeDomain",
    "KNOWLEDGE_SCHEMA",
]
