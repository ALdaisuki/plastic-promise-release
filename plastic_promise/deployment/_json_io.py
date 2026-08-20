"""Small durability primitives owned by the deployment package."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def write_text_atomically(path: Path, content: str) -> None:
    """Publish UTF-8 text only after its complete, fsynced replacement is ready."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def write_json_atomically(path: Path, payload: Mapping[str, object]) -> None:
    """Publish JSON only after its complete, fsynced replacement is ready."""

    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    write_text_atomically(path, f"{rendered}\n")


__all__ = ["write_json_atomically", "write_text_atomically"]
