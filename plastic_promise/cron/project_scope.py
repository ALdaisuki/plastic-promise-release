"""Fail-closed project scope resolution for internal scanners."""

from __future__ import annotations

from plastic_promise.core.project_identity import (
    SYSTEM_GOVERNANCE_PROJECT_ID as _SYSTEM_GOVERNANCE_PROJECT_ID,
)
from plastic_promise.core.project_identity import canonical_project_id

SYSTEM_GOVERNANCE_PROJECT_ID = _SYSTEM_GOVERNANCE_PROJECT_ID


class ProjectScopeResolutionError(RuntimeError):
    """Raised when a project scanner cannot select one unambiguous project."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def normalize_project_id(value: object, *, allow_system: bool = False) -> str:
    """Return one canonical project id or an empty invalid marker."""

    return canonical_project_id(value, allow_system=allow_system)


def list_memory_project_ids(conn) -> tuple[str, ...]:
    """List valid non-system projects represented by canonical memories."""

    rows = conn.execute(
        "SELECT DISTINCT TRIM(project_id) FROM memories "
        "WHERE typeof(project_id) = 'text' "
        "AND project_id = TRIM(project_id) "
        "AND TRIM(project_id) LIKE 'project:%' "
        "ORDER BY TRIM(project_id)"
    ).fetchall()
    return tuple(project_id for row in rows if (project_id := normalize_project_id(row[0])))


def resolve_memory_project_id(
    conn,
    project_id: object = None,
) -> str:
    """Resolve an explicit scope or the database's sole valid memory project."""

    if project_id is not None:
        explicit = normalize_project_id(project_id)
        if explicit:
            return explicit
        raise ProjectScopeResolutionError("project_scope_invalid")
    projects = list_memory_project_ids(conn)
    if len(projects) == 1:
        return projects[0]
    if not projects:
        raise ProjectScopeResolutionError("project_scope_unavailable")
    raise ProjectScopeResolutionError("project_scope_ambiguous")
