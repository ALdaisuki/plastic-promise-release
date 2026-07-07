from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_PROJECT_POLICIES = {"strict", "balanced", "open"}
VALID_VISIBILITIES = {"project", "global", "shared", "private"}
TELEMETRY_SOURCES = {"maintenance_daemon", "skill_session", "step_auditor"}


@dataclass
class ProjectContext:
    project_id: str
    project_policy: str = "balanced"
    visibility: str = "project"
    source_class: str = "experience"
    degraded: bool = False
    warnings: list[str] | None = None

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []

    def warning_list(self) -> list[str]:
        return list(self.warnings or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_policy": self.project_policy,
            "visibility": self.visibility,
            "source_class": self.source_class,
            "degraded": self.degraded,
            "warnings": self.warning_list(),
        }


def infer_project_context(args: dict[str, Any] | None) -> ProjectContext:
    values = args or {}
    warnings: list[str] = []
    visibility = str(values.get("visibility") or "project")

    project_id = _infer_project_id(values)
    degraded = project_id == "project:unknown"
    if degraded:
        warnings.append("project_id unresolved; using project:unknown")

    if values.get("scope") == "global" and "visibility" not in values:
        visibility = "global"

    project_policy = str(values.get("project_policy") or "balanced")
    if project_policy not in VALID_PROJECT_POLICIES:
        warnings.append(
            f"invalid project_policy {project_policy!r}; using balanced"
        )
        project_policy = "balanced"

    if visibility not in VALID_VISIBILITIES:
        warnings.append(f"invalid visibility {visibility!r}; using project")
        visibility = "project"

    source_class = str(
        values.get("source_class")
        or source_class_from_inputs(
            values.get("source"),
            values.get("memory_type"),
            values.get("tags"),
        )
    )

    return ProjectContext(
        project_id=project_id,
        project_policy=project_policy,
        visibility=visibility,
        source_class=source_class,
        degraded=degraded,
        warnings=warnings,
    )


def source_class_from_inputs(
    source: Any,
    memory_type: Any,
    tags: list[str] | None,
) -> str:
    tag_values = [str(tag) for tag in tags or []]
    memory_type_value = str(memory_type or "")
    source_value = str(source or "")

    if any(tag in {"prompt", "review:prompt"} for tag in tag_values):
        return "prompt"
    if source_value in TELEMETRY_SOURCES:
        return "telemetry"
    if memory_type_value == "code":
        return "code_fact"
    if source_value == "user":
        return "user_fact"
    if memory_type_value in {"reflection", "improvement"}:
        return "reflection"
    return memory_type_value or "experience"


def _infer_project_id(values: dict[str, Any]) -> str:
    explicit_project_id = values.get("project_id")
    if explicit_project_id:
        return _normalize_project_id(str(explicit_project_id))

    for tag in values.get("tags") or []:
        tag_value = str(tag)
        if tag_value.startswith("project:"):
            return tag_value

    scope = str(values.get("scope") or "")
    if scope.startswith("agent:"):
        return f"project:{scope}"
    if scope == "global":
        return "project:legacy-global"

    return "project:unknown"


def _normalize_project_id(project_id: str) -> str:
    if project_id.startswith("project:"):
        return project_id
    return f"project:{project_id}"
