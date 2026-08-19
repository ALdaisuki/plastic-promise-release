"""Deterministic knowledge ingestion coordinator.

Callers submit a source and receive a Submission; they never orchestrate
parsing, chunking, or lifecycle transitions.  Identical bytes submitted to
the same Source reuse the existing immutable version.
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING

from plastic_promise.core.chunking import build_chunk_manifest
from plastic_promise.knowledge.adapters.parser_markdown import (
    MarkdownTextParser,
    MarkdownTextParserError,
)
from plastic_promise.knowledge.blobs import BlobStore, BlobStoreError
from plastic_promise.knowledge.contracts import (
    MAX_KNOWN_SOURCE_KINDS,
    JobView,
    SourceView,
    Submission,
    VersionView,
)

if TYPE_CHECKING:
    from plastic_promise.knowledge.repository import KnowledgeRepository

_DEFAULT_CHUNK_TARGET_CHARS = 1200
_DEFAULT_CHUNK_HARD_CHARS = 2400
_DEFAULT_LEASE_SECONDS = 300


class KnowledgeIngestionError(RuntimeError):
    """Raised for deterministic ingestion failures."""


class IngestCoordinator:
    """Owns the source submission and job lifecycle state machine."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        blobs: BlobStore,
        parser: MarkdownTextParser | None = None,
        *,
        actor: str = "system",
    ) -> None:
        self._repository = repository
        self._blobs = blobs
        self._parser = parser or MarkdownTextParser()
        self._actor = actor

    # -- external seam ----------------------------------------------------

    def submit_source(
        self,
        project_id: str,
        content: bytes,
        *,
        source_name: str,
        space_name: str = "default",
        kind: str = "upload",
        origin_ref: str | None = None,
        actor: str | None = None,
        space_description: str = "",
    ) -> Submission:
        """Submit source bytes for deterministic ingestion.

        Identical bytes for the same Source reuse the existing immutable
        version and do not create a duplicate job.
        """
        if kind not in MAX_KNOWN_SOURCE_KINDS:
            raise KnowledgeIngestionError(f"unsupported source kind: {kind}")
        if not isinstance(content, bytes) or not content:
            raise KnowledgeIngestionError("source content must be non-empty bytes")
        normalized_project_id = (project_id or "").strip()
        if not normalized_project_id or normalized_project_id.casefold() in {
            "project:unknown",
            "unknown",
        }:
            raise KnowledgeIngestionError("project_id is required")
        project_id = normalized_project_id
        if not (source_name or "").strip():
            raise KnowledgeIngestionError("source_name is required")

        self._repository.init_schema()
        effective_actor = actor or self._actor
        space_id = self._repository.get_or_create_space(project_id, space_name, space_description)
        source = self._get_or_create_source(
            project_id=project_id,
            space_id=space_id,
            kind=kind,
            name=source_name.strip(),
            origin_ref=origin_ref,
            actor=effective_actor,
        )
        content_hash = hashlib.sha256(content).hexdigest()
        existing = self._repository.find_version_by_content_hash(source.id, content_hash)
        if existing is not None:
            self._repository.audit(
                project_id=project_id,
                actor=effective_actor,
                action="submit_reused",
                object_type="source_version",
                object_id=existing.id,
                detail={"content_hash": content_hash[:16], "source_id": source.id},
            )
            return Submission(
                job_id="",
                source_id=source.id,
                reused_version_id=existing.id,
                status="done_reused",
            )

        job = self._repository.create_job(project_id=project_id, source_id=source.id, stage="parse")
        if not self._repository.claim_job(job.id, owner=self._actor):
            raise KnowledgeIngestionError(f"job could not be claimed: {job.id}")
        try:
            self._ingest(
                job_id=job.id,
                source=source,
                content=content,
                content_hash=content_hash,
                actor=effective_actor,
            )
        except (MarkdownTextParserError, BlobStoreError, KnowledgeIngestionError) as exc:
            self._repository.finish_job(job.id, status="failed", error=str(exc))
            return Submission(
                job_id=job.id, source_id=source.id, reused_version_id=None, status="failed"
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._repository.finish_job(
                job.id, status="failed", error=f"{type(exc).__name__}: {exc}"
            )
            return Submission(
                job_id=job.id, source_id=source.id, reused_version_id=None, status="failed"
            )
        return Submission(job_id=job.id, source_id=source.id, reused_version_id=None, status="done")

    # -- internal pipeline -------------------------------------------------

    def _ingest(
        self,
        *,
        job_id: str,
        source: SourceView,
        content: bytes,
        content_hash: str,
        actor: str,
    ) -> None:
        document = self._parser.parse(content)
        target_chars = int(
            os.environ.get("PP_KNOWLEDGE_CHUNK_TARGET_CHARS", str(_DEFAULT_CHUNK_TARGET_CHARS))
        )
        hard_chars = int(
            os.environ.get("PP_KNOWLEDGE_CHUNK_HARD_CHARS", str(_DEFAULT_CHUNK_HARD_CHARS))
        )
        manifest = build_chunk_manifest(
            document.text,
            target_chars=target_chars,
            hard_chars=hard_chars,
        )
        blob_ref = self._blobs.put(content)
        version = self._repository.create_version(
            source_id=source.id,
            content_hash=content_hash,
            blob_sha256=blob_ref.sha256,
            byte_size=blob_ref.byte_size,
            parser_id=document.parser_id,
            parse_schema=document.parse_schema,
            document_title=document.title,
            structure_manifest=manifest,
        )
        chunk_count = self._repository.insert_chunks(version.id, list(manifest["chunks"]))
        self._repository.supersede_older_versions(source.id, keep_version_id=version.id)
        outbox_id = self._repository.enqueue_index(version.id, op="index")
        self._repository.audit(
            project_id=source.project_id,
            actor=actor,
            action="source_version_admitted",
            object_type="source_version",
            object_id=version.id,
            detail={
                "source_id": source.id,
                "chunk_count": chunk_count,
                "blob_sha256": blob_ref.sha256[:16],
                "outbox_id": outbox_id,
            },
        )
        self._repository.finish_job(
            job_id,
            status="done",
            result={
                "version_id": version.id,
                "version_no": version.version_no,
                "chunk_count": chunk_count,
                "outbox_id": outbox_id,
            },
        )

    def _get_or_create_source(
        self,
        *,
        project_id: str,
        space_id: str,
        kind: str,
        name: str,
        origin_ref: str | None,
        actor: str,
    ) -> SourceView:
        for source in self._repository.list_sources(project_id, limit=1000):
            if source.space_id == space_id and source.name == name and source.kind == kind:
                return source
        source = self._repository.create_source(
            project_id=project_id,
            space_id=space_id,
            kind=kind,
            name=name,
            origin_ref=origin_ref,
        )
        self._repository.audit(
            project_id=project_id,
            actor=actor,
            action="source_created",
            object_type="source",
            object_id=source.id,
        )
        return source

    # -- read seam --------------------------------------------------------

    def get_job(self, job_id: str) -> JobView:
        return self._repository.get_job(job_id)

    def cancel_job(self, job_id: str) -> bool:
        return self._repository.cancel_job(job_id)

    def list_jobs(
        self, project_id: str, *, status: str | None = None, limit: int = 100
    ) -> list[JobView]:
        return self._repository.list_jobs(project_id, status=status, limit=limit)

    def list_sources(self, project_id: str, *, limit: int = 100) -> list[SourceView]:
        return self._repository.list_sources(project_id, limit=limit)

    def get_versions(self, source_id: str) -> list[VersionView]:
        return self._repository.list_versions(source_id)
