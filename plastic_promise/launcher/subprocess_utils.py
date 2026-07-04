"""Subprocess helpers for the One-Click Launcher."""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def hidden_subprocess_kwargs(*, new_process_group: bool = False) -> dict[str, Any]:
    """Return Windows-only kwargs that keep launcher child processes hidden."""
    if sys.platform != "win32":
        return {}

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if new_process_group:
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    kwargs: dict[str, Any] = {"creationflags": creationflags}

    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo

    return kwargs
