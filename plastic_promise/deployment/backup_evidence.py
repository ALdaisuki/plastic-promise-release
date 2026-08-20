"""Verified, non-secret provenance for controller-created SQLite backups."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._json_io import write_json_atomically

if TYPE_CHECKING:
    from pathlib import Path

BACKUP_EVIDENCE_SCHEMA_VERSION = "plastic-promise/deployment-backup-evidence/v1"
_HASH_CHUNK_BYTES = 1024 * 1024
_EVIDENCE_FIELDS = {"schema", "profile", "sha256", "bytes"}


@dataclass(frozen=True)
class BackupEvidence:
    """Verified provenance for one standalone SQLite backup file."""

    profile_id: str
    content_sha256: str
    byte_count: int
    evidence_sha256: str


class BackupEvidenceError(ValueError):
    """Stable, non-sensitive reason why a restore source is not trusted."""


def backup_evidence_path(backup_path: Path) -> Path:
    """Return the exact sidecar controlled by this deployment layer."""

    return backup_path.with_name(f"{backup_path.name}.evidence.json")


def _sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}", path.stat().st_size


def _evidence_digest(payload: dict[str, object]) -> str:
    return _sha256_bytes(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def write_backup_evidence(backup_path: Path, *, profile_id: str) -> BackupEvidence:
    """Bind a controller-created backup to its profile and exact file content."""

    if not backup_path.is_file():
        raise BackupEvidenceError("backup_evidence_source_missing")
    if not isinstance(profile_id, str) or not profile_id:
        raise BackupEvidenceError("backup_evidence_profile_invalid")
    content_sha256, byte_count = _sha256_file(backup_path)
    payload: dict[str, object] = {
        "schema": BACKUP_EVIDENCE_SCHEMA_VERSION,
        "profile": profile_id,
        "sha256": content_sha256,
        "bytes": byte_count,
    }
    write_json_atomically(backup_evidence_path(backup_path), payload)
    return BackupEvidence(
        profile_id=profile_id,
        content_sha256=content_sha256,
        byte_count=byte_count,
        evidence_sha256=_evidence_digest(payload),
    )


def load_verified_backup_evidence(
    backup_path: Path,
    *,
    expected_profile_id: str | None = None,
) -> BackupEvidence:
    """Validate provenance before a backup can become a restore source."""

    if not backup_path.is_file():
        raise BackupEvidenceError("restore_source_missing")
    evidence_path = backup_evidence_path(backup_path)
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BackupEvidenceError("restore_source_evidence_missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupEvidenceError("restore_source_evidence_unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != _EVIDENCE_FIELDS:
        raise BackupEvidenceError("restore_source_evidence_invalid")
    if payload.get("schema") != BACKUP_EVIDENCE_SCHEMA_VERSION:
        raise BackupEvidenceError("restore_source_evidence_schema_invalid")
    profile_id = payload.get("profile")
    declared_digest = payload.get("sha256")
    declared_bytes = payload.get("bytes")
    if (
        not isinstance(profile_id, str)
        or not profile_id
        or not isinstance(declared_digest, str)
        or not declared_digest.startswith("sha256:")
        or isinstance(declared_bytes, bool)
        or not isinstance(declared_bytes, int)
        or declared_bytes < 0
    ):
        raise BackupEvidenceError("restore_source_evidence_invalid")
    if expected_profile_id is not None and profile_id != expected_profile_id:
        raise BackupEvidenceError("cross_profile_restore_requires_migration")
    content_sha256, byte_count = _sha256_file(backup_path)
    if content_sha256 != declared_digest or byte_count != declared_bytes:
        raise BackupEvidenceError("restore_source_evidence_hash_mismatch")
    return BackupEvidence(
        profile_id=profile_id,
        content_sha256=content_sha256,
        byte_count=byte_count,
        evidence_sha256=_evidence_digest(payload),
    )


__all__ = [
    "BACKUP_EVIDENCE_SCHEMA_VERSION",
    "BackupEvidence",
    "BackupEvidenceError",
    "backup_evidence_path",
    "load_verified_backup_evidence",
    "write_backup_evidence",
]
