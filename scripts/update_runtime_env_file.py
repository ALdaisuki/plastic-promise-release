#!/usr/bin/env python3
"""Atomically update exact EnvironmentFile keys without changing ownership."""

from __future__ import annotations

import argparse
import os
import re
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

ENV_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*$")


def parse_assignment(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("runtime_env_assignment_invalid")
    key, raw = value.split("=", 1)
    if not ENV_NAME.fullmatch(key) or "\x00" in raw or "\r" in raw or "\n" in raw:
        raise argparse.ArgumentTypeError("runtime_env_assignment_invalid")
    return key, raw


def update_lines(lines: list[str], assignments: list[tuple[str, str]]) -> list[str]:
    pending = dict(assignments)
    emitted: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0] if "=" in stripped else ""
        if key in pending:
            if key not in emitted:
                output.append(f"{key}={pending[key]}")
                emitted.add(key)
            continue
        output.append(line)
    for key, value in assignments:
        if key not in emitted:
            output.append(f"{key}={value}")
            emitted.add(key)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--owner-reference", type=Path)
    parser.add_argument("--set", dest="assignments", type=parse_assignment, action="append", required=True)
    args = parser.parse_args()

    path = args.env_file
    if path.is_symlink():
        raise SystemExit("runtime_env_file_missing_or_unsafe")
    if path.exists():
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SystemExit("runtime_env_file_missing_or_unsafe")
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        if args.owner_reference is None:
            raise SystemExit("runtime_env_owner_reference_required")
        reference = args.owner_reference
        if not reference.exists() or reference.is_symlink():
            raise SystemExit("runtime_env_owner_reference_missing_or_unsafe")
        metadata = reference.stat()
        lines = []

    if (
        os.name == "posix"
        and os.geteuid() != 0
        and (os.geteuid() != metadata.st_uid or os.getegid() != metadata.st_gid)
    ):
        raise SystemExit("runtime_env_owner_mismatch")

    rendered = "\n".join(update_lines(lines, args.assignments)) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if os.name == "posix" and os.geteuid() == 0:
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
    print(f"runtime_env_updated={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
