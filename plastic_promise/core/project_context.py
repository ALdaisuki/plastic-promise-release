from __future__ import annotations

import configparser
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

VALID_PROJECT_POLICIES = {"strict", "balanced", "open"}
VALID_VISIBILITIES = {"project", "global", "shared", "private"}
TELEMETRY_SOURCES = {"maintenance_daemon", "skill_session", "step_auditor"}
PROJECT_ID_ENV_KEYS = ("PLASTIC_PROJECT_ID", "PP_PROJECT_ID")
_REPO_COMPONENT_RE = re.compile(r"[^a-z0-9._-]+")


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
        warnings.append(f"invalid project_policy {project_policy!r}; using balanced")
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

    env_project_id = _infer_env_project_id()
    if env_project_id:
        return env_project_id

    return "project:unknown"


def _normalize_project_id(project_id: str) -> str:
    project_id = project_id.strip()
    if project_id.startswith("project:"):
        return project_id
    return f"project:{project_id}"


def _infer_env_project_id() -> str:
    for key in PROJECT_ID_ENV_KEYS:
        value = os.environ.get(key, "")
        if value and value.strip():
            return _normalize_project_id(value)
    return ""


def infer_repository_project_id(cwd: str | os.PathLike[str] | None) -> str:
    """Derive a clone-stable project id from origin, with a local path fallback."""

    root = _repository_root(Path(cwd).expanduser() if cwd else Path.cwd())
    remote = _origin_remote(root)
    remote_identity = _remote_identity(remote)
    if remote_identity:
        return f"project:repo:{remote_identity}"
    resolved = str(root.resolve(strict=False))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    label = _REPO_COMPONENT_RE.sub("-", root.name.casefold()).strip("-._") or "repository"
    return f"project:local:{label[:48]}-{digest}"


def _repository_root(start: Path) -> Path:
    candidate = start if start.is_dir() else start.parent
    for path in (candidate, *candidate.parents):
        if (path / ".git").exists():
            return path
    return candidate


def _origin_remote(root: Path) -> str:
    git_marker = root / ".git"
    config_paths: list[Path] = []
    if git_marker.is_dir():
        config_paths.append(git_marker / "config")
    elif git_marker.is_file():
        try:
            marker = git_marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            marker = ""
        if marker.casefold().startswith("gitdir:"):
            git_dir = Path(marker.split(":", 1)[1].strip()).expanduser()
            if not git_dir.is_absolute():
                git_dir = root / git_dir
            config_paths.extend(
                (
                    git_dir / "config",
                    git_dir.parent.parent / "config",
                )
            )
    parser = configparser.ConfigParser(interpolation=None)
    for config_path in config_paths:
        try:
            with config_path.open(encoding="utf-8", errors="replace") as handle:
                parser.read_file(handle)
        except (OSError, configparser.Error):
            continue
        if parser.has_option('remote "origin"', "url"):
            return parser.get('remote "origin"', "url").strip()
    return ""


def _remote_identity(remote: str) -> str:
    value = str(remote or "").strip()
    if not value:
        return ""
    host = ""
    path = ""
    if "://" in value:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        host = str(parsed.hostname or "").casefold()
        path = parsed.path
    else:
        match = re.fullmatch(r"(?:[^@\s]+@)?(?P<host>[^:\s]+):(?P<path>.+)", value)
        if not match:
            return ""
        host = match.group("host").casefold()
        path = match.group("path")
    parts = [
        _REPO_COMPONENT_RE.sub("-", part.casefold()).strip("-._")
        for part in path.strip("/").removesuffix(".git").split("/")
    ]
    parts = [part for part in parts if part]
    safe_host = _REPO_COMPONENT_RE.sub("-", host).strip("-._")
    if not safe_host or len(parts) < 2:
        return ""
    return "/".join((safe_host, parts[-2], parts[-1]))[:180]
