"""Exclusive host-state lease for a canonical MCP runtime process.

The native systemd and containerized server templates deliberately share one
lock file inside the server-owned state directory.  That prevents an operator
from opening the same SQLite canonical store through both templates at once.
The lock descriptor is made inheritable before ``exec`` so it remains held for
the lifetime of the MCP server process.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import TYPE_CHECKING

if os.name == "nt":  # pragma: no cover - exercised on Windows CI.
    import msvcrt
else:  # pragma: no cover - the active POSIX backend is covered below.
    import fcntl

if TYPE_CHECKING:
    from collections.abc import Mapping

LOCK_HELD_EXIT_STATUS = 75
_DEFAULT_DATABASE_PATH = Path("data/db/plastic_memory.db")


class CanonicalRuntimeLockHeldError(RuntimeError):
    """Raised when another native or Compose MCP runtime owns the state lease."""


def runtime_lock_path(environment: Mapping[str, str] | None = None) -> Path:
    """Return the state-root lock path from environment without reading SQLite."""

    values = os.environ if environment is None else environment
    configured = values.get("PLASTIC_RUNTIME_LOCK_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    database_path = Path(values.get("PLASTIC_DB_PATH", str(_DEFAULT_DATABASE_PATH))).expanduser()
    return database_path.parent.parent / "runtime" / "mcp.lock"


def acquire_canonical_runtime_lock(path: Path) -> int:
    """Acquire a non-blocking, process-lifetime exclusive state lease."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            _acquire_windows_lock(descriptor)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        os.close(descriptor)
        if isinstance(exc, BlockingIOError) or exc.errno in {
            errno.EACCES,
            errno.EAGAIN,
            errno.EDEADLK,
        }:
            raise CanonicalRuntimeLockHeldError("canonical_runtime_lock_held") from exc
        raise
    os.set_inheritable(descriptor, True)
    return descriptor


def _acquire_windows_lock(descriptor: int) -> None:
    """Lock one byte for the process lifetime using the Windows CRT."""

    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
    os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        # The CRT reports a held byte as EACCES/EAGAIN depending on the
        # Windows version.  Preserve all other I/O failures for diagnostics.
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise BlockingIOError(errno.EAGAIN, "canonical runtime lock held") from exc
        raise


def main() -> int:
    """Hold the state lease, then replace this process with the MCP server."""

    try:
        acquire_canonical_runtime_lock(runtime_lock_path())
    except CanonicalRuntimeLockHeldError:
        print("canonical_runtime_lock_held", flush=True)
        return LOCK_HELD_EXIT_STATUS
    os.execvp("plastic-promise-streamable-http", ["plastic-promise-streamable-http"])
    raise AssertionError("os.execvp must not return")


if __name__ == "__main__":
    raise SystemExit(main())
