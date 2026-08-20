"""Knowledge truth store: sources, versions, evidence chunks, and lexical retrieval."""

from plastic_promise.knowledge.blobs import (
    BlobStore,
    BlobStoreError,
    FilesystemBlobStore,
    MemoryBlobStore,
)
from plastic_promise.knowledge.contracts import (
    BlobRef,
    ChunkHit,
    JobView,
    NormalizedDocument,
    QueryResult,
    SourceView,
    Submission,
    VersionView,
)
from plastic_promise.knowledge.ingestion import IngestCoordinator, KnowledgeIngestionError
from plastic_promise.knowledge.query import LexicalKnowledgeQuery, tokenize
from plastic_promise.knowledge.repository import KnowledgeRepository

__all__ = [
    "BlobRef",
    "BlobStore",
    "BlobStoreError",
    "ChunkHit",
    "FilesystemBlobStore",
    "IngestCoordinator",
    "JobView",
    "KnowledgeIngestionError",
    "KnowledgeRepository",
    "LexicalKnowledgeQuery",
    "MemoryBlobStore",
    "NormalizedDocument",
    "QueryResult",
    "SourceView",
    "Submission",
    "VersionView",
    "tokenize",
]
