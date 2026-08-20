"""Isolated knowledge restore drill (slice-1 gate before vector search).

Restores a knowledge backup into a throwaway directory, then verifies SQLite
integrity, the restored digest, referenced blob hashes, and a lexical query
smoke.  Nothing in the production store is mutated.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from plastic_promise.knowledge.blobs import FilesystemBlobStore
from plastic_promise.knowledge.contracts import knowledge_blob_root
from plastic_promise.knowledge.query import LexicalKnowledgeQuery
from plastic_promise.knowledge.repository import KnowledgeRepository


def restore_smoke_evidence(
    backup_path: str | Path,
    *,
    db_path: str | Path | None = None,
    blob_root: str | Path | None = None,
    project_id: str = "project:plastic-promise",
    probe: str = "",
    keep: bool = False,
) -> dict[str, Any]:
    """Restore ``backup_path`` into an isolated temp dir and run the drill.

    Returns evidence JSON; the restored directory is retained (or removed when
    ``keep`` is false and every check passed).
    """
    backup = Path(backup_path).expanduser().resolve()
    if not backup.is_file():
        raise FileNotFoundError(f"knowledge backup not found: {backup}")

    restored_dir = Path(tempfile.mkdtemp(prefix="knowledge-restore-"))
    restored_db = restored_dir / "plastic_knowledge.db"
    try:
        _restore(backup, restored_db)
        integrity = _integrity_check(restored_db)
        backup_digest = _sha256(backup)
        restored_digest = _sha256(restored_db)
        blobs = FilesystemBlobStore(blob_root or knowledge_blob_root())
        verified, missing = _verify_referenced_blobs(restored_db, blobs)
        probe_text = probe or _first_chunk_text(restored_db)
        repository = KnowledgeRepository(restored_db, read_only=True)
        result = LexicalKnowledgeQuery(repository).search(project_id, probe_text, limit=3)
        ok = integrity == "ok" and backup_digest == restored_digest and not missing
        evidence: dict[str, Any] = {
            "ok": ok,
            "backup": str(backup),
            "backup_sha256": backup_digest,
            "restore_sha256": restored_digest,
            "integrity_check": integrity,
            "blobs_verified": verified,
            "blobs_missing": missing[:5],
            "probe_query": probe_text[:120],
            "probe_total_hits": result.total_hits,
            "probe_hits": [hit.chunk_id for hit in result.hits[:3]],
            "restored_dir": str(restored_dir),
            "note": "isolated drill; production store untouched",
        }
        if ok and not keep:
            shutil.rmtree(restored_dir, ignore_errors=True)
            evidence["restored_dir"] = ""
        return evidence
    except BaseException:
        shutil.rmtree(restored_dir, ignore_errors=True)
        raise


def _restore(backup: Path, target: Path) -> None:
    source_connection = sqlite3.connect(str(backup))
    target_connection = sqlite3.connect(str(target))
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def _integrity_check(db_path: Path) -> str:
    with sqlite3.connect(str(db_path)) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    return str(row[0])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_referenced_blobs(
    db_path: Path,
    blobs: FilesystemBlobStore,
) -> tuple[int, list[str]]:
    with sqlite3.connect(str(db_path)) as connection:
        rows = connection.execute(
            "SELECT DISTINCT blob_sha256 FROM knowledge_source_versions"
        ).fetchall()
    verified = 0
    missing: list[str] = []
    for row in rows:
        digest = str(row[0])
        if blobs.has(digest):
            verified += 1
        else:
            missing.append(digest)
    return verified, missing


def _first_chunk_text(db_path: Path) -> str:
    with sqlite3.connect(str(db_path)) as connection:
        row = connection.execute(
            "SELECT text FROM knowledge_chunks"
            " WHERE status='active' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    if row is None:
        return ""
    return " ".join(str(row[0]).split())[:200]
