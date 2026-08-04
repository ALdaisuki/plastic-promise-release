"""Create one-time control-plane tokens and a digest-only EnvironmentFile."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import secrets
import stat
import sys
from pathlib import Path

_ROLES = (
    ("viewer", "PP_CONTROL_VIEWER_TOKEN_SHA256"),
    ("operator", "PP_CONTROL_OPERATOR_TOKEN_SHA256"),
    ("secret-admin", "PP_CONTROL_SECRET_ADMIN_TOKEN_SHA256"),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _effective_uid() -> int:
    try:
        return os.geteuid()
    except AttributeError as exc:  # pragma: no cover - this utility targets POSIX hosts
        raise OSError("credential directory ownership checks require a POSIX host") from exc


def _validate_private_directory(path: Path, metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise PermissionError(f"credential directory must not be a symlink: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"credential parent is not a directory: {path}")
    if metadata.st_uid != _effective_uid():
        raise PermissionError(f"credential directory is not owned by the current user: {path}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError(f"credential directory grants group or other access: {path}")


def _prepare_private_parent(parent: Path) -> None:
    missing: list[Path] = []
    cursor = parent
    while True:
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            next_cursor = cursor.parent
            if next_cursor == cursor:
                raise
            cursor = next_cursor
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise PermissionError(f"credential directory ancestor must not be a symlink: {cursor}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(f"credential directory ancestor is not a directory: {cursor}")
        break

    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            created = False
        else:
            created = True

        metadata = directory.lstat()
        _validate_private_directory(directory, metadata)
        if created:
            directory.chmod(0o700)
            _validate_private_directory(directory, directory.lstat())

    _validate_private_directory(parent, parent.lstat())


def _open_private_parent(parent: Path) -> int:
    _prepare_private_parent(parent)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent, flags)
    try:
        _validate_private_directory(parent, os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _write_exclusive(path: Path, content: str) -> None:
    path = path.expanduser()
    parent_descriptor = _open_private_parent(path.parent)
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "w", encoding="ascii", newline="\n")
        descriptor = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        os.close(parent_descriptor)


def create_credentials(output: Path) -> dict[str, str]:
    tokens = {role: secrets.token_urlsafe(48) for role, _env_name in _ROLES}
    lines = []
    for role, env_name in _ROLES:
        digest = hashlib.sha256(tokens[role].encode("ascii")).hexdigest()
        lines.append(f"{env_name}={digest}")
    _write_exclusive(output, "\n".join(lines) + "\n")
    return tokens


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        tokens = create_credentials(arguments.output)
    except (FileExistsError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Digest-only credentials written to {arguments.output.expanduser()}")
    print("Control tokens (shown once):")
    for role, _env_name in _ROLES:
        print(f"{role}: {tokens[role]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
