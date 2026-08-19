#!/usr/bin/env python3
"""Execute a command with safely parsed EnvironmentFile-style inputs."""

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"environment_file_missing_or_unsafe:{path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"environment_file_line_invalid:{path}")
        key, value = line.split("=", 1)
        if not key or not key.replace("_", "A").isalnum() or not key[0].isalpha():
            raise SystemExit(f"environment_file_key_invalid:{path}")
        if "\x00" in value:
            raise SystemExit(f"environment_file_value_invalid:{path}")
        stripped_value = value.strip()
        if not stripped_value:
            values[key] = ""
            continue
        if stripped_value[0] not in {"'", '"'}:
            values[key] = stripped_value
            continue
        try:
            decoded = shlex.split(stripped_value, comments=False, posix=True)
        except ValueError:
            raise SystemExit(f"environment_file_value_invalid:{path}") from None
        if len(decoded) == 1:
            values[key] = decoded[0]
        elif not decoded and stripped_value in {"''", '""'}:
            values[key] = ""
        else:
            raise SystemExit(f"environment_file_value_invalid:{path}")
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    parser.add_argument(
        "--owner-reference",
        type=Path,
        help="Drop root privileges to the owner of this non-symlink path before exec",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise SystemExit("environment_command_missing")
    environ = dict(os.environ)
    for path in args.env_file:
        environ.update(parse_env_file(path))
    if args.owner_reference is not None:
        reference = args.owner_reference
        if not reference.exists() or reference.is_symlink():
            raise SystemExit("environment_owner_reference_missing_or_unsafe")
        metadata = reference.stat()
        if os.name != "posix":
            raise SystemExit("environment_owner_switch_unsupported")
        if os.geteuid() != 0 and (
            os.geteuid() != metadata.st_uid or os.getegid() != metadata.st_gid
        ):
            raise SystemExit("environment_owner_switch_requires_root")
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(metadata.st_gid)
            os.setuid(metadata.st_uid)
    os.execvpe(command[0], command, environ)
    raise AssertionError("os.execvpe returned")


if __name__ == "__main__":
    raise SystemExit(main())
