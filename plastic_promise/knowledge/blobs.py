"""Content-addressed blob storage with atomic writes.

Blobs are immutable and referenced only by their SHA-256 digest.  The
database commits a reference to an admitted Blob; identical bytes never
create a second Blob, but the caller decides how many Source identities
reference it.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from plastic_promise.knowledge.contracts import BlobRef


class BlobStoreError(RuntimeError):
    """Raised when a Blob cannot be read or persisted."""


class BlobStore(ABC):
    """Content-addressed immutable byte store."""

    @abstractmethod
    def put(self, data: bytes) -> BlobRef:
        """Persist bytes and return their content-addressed reference."""

    @abstractmethod
    def read(self, sha256: str) -> bytes:
        """Return the bytes for a previously admitted digest."""

    @abstractmethod
    def has(self, sha256: str) -> bool:
        """Return whether a digest is present."""

    def counts(self) -> dict[str, int]:
        """Return bounded diagnostic counters (default: none)."""
        return {}


class FilesystemBlobStore(BlobStore):
    """SHA-256 content-addressed store with atomic temp -> fsync -> rename."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()

    def _path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise BlobStoreError("invalid blob digest")
        return self._root / sha256[:2] / sha256[2:]

    def put(self, data: bytes) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        target = self._path(digest)
        if target.is_file() and target.stat().st_size == len(data):
            return BlobRef(sha256=digest, byte_size=len(data))
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=".blob-", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            _fsync_directory(target.parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
        return BlobRef(sha256=digest, byte_size=len(data))

    def read(self, sha256: str) -> bytes:
        target = self._path(sha256)
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise BlobStoreError(f"blob missing: {sha256}") from exc

    def has(self, sha256: str) -> bool:
        try:
            return self._path(sha256).is_file()
        except BlobStoreError:
            return False


class MemoryBlobStore(BlobStore):
    """In-memory adapter for tests and deterministic smoke runs."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        self._blobs[digest] = data
        return BlobRef(sha256=digest, byte_size=len(data))

    def read(self, sha256: str) -> bytes:
        try:
            return self._blobs[sha256]
        except KeyError as exc:
            raise BlobStoreError(f"blob missing: {sha256}") from exc

    def has(self, sha256: str) -> bool:
        return sha256 in self._blobs

    def counts(self) -> dict[str, int]:
        return {"blobs": len(self._blobs)}


def _fsync_directory(directory: Path) -> None:
    """Best-effort fsync of a directory so the rename is durable."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass
