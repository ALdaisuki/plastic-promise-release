"""Canonical project identity validation shared by project-scoped runtimes."""

from __future__ import annotations

import re

PROJECT_ID_RE = re.compile(r"\Aproject:[A-Za-z0-9][A-Za-z0-9_.:/-]{0,247}\Z")
SYSTEM_GOVERNANCE_PROJECT_ID = "project:system-governance"
LEGACY_GLOBAL_PROJECT_ID = "project:legacy-global"
LEGACY_QUARANTINE_PROJECT_ID = "project:legacy-quarantine"

_UNKNOWN_PROJECT_IDS = frozenset({"unknown", "project:unknown"})
_LEGACY_PROJECT_IDS = frozenset(
    {
        LEGACY_GLOBAL_PROJECT_ID,
        LEGACY_QUARANTINE_PROJECT_ID,
    }
)


def canonical_project_id(
    value: object,
    *,
    allow_system: bool = True,
    allow_legacy: bool = False,
) -> str:
    """Return an unchanged canonical project id or an empty fail-closed marker."""

    if not isinstance(value, str) or value != value.strip() or not PROJECT_ID_RE.fullmatch(value):
        return ""
    normalized = value.casefold()
    if normalized in _UNKNOWN_PROJECT_IDS:
        return ""
    if not allow_system and normalized == SYSTEM_GOVERNANCE_PROJECT_ID:
        return ""
    if not allow_legacy and normalized in _LEGACY_PROJECT_IDS:
        return ""
    return value
