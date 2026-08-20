#!/usr/bin/env python3
"""Atomically rotate one server-side compute-node Bearer authorization."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import tempfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

AUTH_NAME = re.compile(r"PP_NODE_AUTH_[A-Z0-9_]{1,96}$")
AUTH_VALUE = re.compile(r"Bearer [A-Za-z0-9._~+/=-]{1,4096}$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--env-name", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    if not AUTH_NAME.fullmatch(args.env_name):
        raise SystemExit("compute_node_authorization_name_invalid")
    for path, error in (
        (args.env_file, "compute_node_env_file_missing_or_unsafe"),
        (args.token_file, "compute_node_token_file_missing_or_unsafe"),
    ):
        if not path.is_file() or path.is_symlink():
            raise SystemExit(error)
    authorization = args.token_file.read_text(encoding="utf-8").strip()
    if not AUTH_VALUE.fullmatch(authorization):
        raise SystemExit("compute_node_authorization_invalid")

    lines = args.env_file.read_text(encoding="utf-8").splitlines()
    replacement = f"{args.env_name}={authorization}"
    output: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(args.env_name + "="):
            if not replaced:
                output.append(replacement)
                replaced = True
            continue
        output.append(line)
    if not replaced:
        output.append(replacement)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path(str(args.env_file) + f".pre-node-auth-rotation-{stamp}")
    shutil.copy2(args.env_file, backup)
    os.chmod(backup, 0o600)
    metadata = args.env_file.stat()
    if os.name == "posix":
        os.chown(backup, metadata.st_uid, metadata.st_gid)
    fd, temporary = tempfile.mkstemp(prefix=".node-auth-", dir=args.env_file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if os.name == "posix":
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, args.env_file)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
    args.token_file.unlink()
    print(f"compute_node_authorization_rotated={args.env_name}")
    print(f"backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
