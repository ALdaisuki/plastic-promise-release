#!/usr/bin/env python3
"""Create a safe, auditable Docker cleanup boundary before an OCI build.

The script deliberately scopes cleanup to Plastic Promise images and Docker's
own unused build cache. It never prunes containers, volumes, networks, model
mounts, databases, or images belonging to another project. The default is a
dry-run report; build workflows use ``--execute`` immediately before building.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TextIO


SCHEMA_VERSION = "plastic-promise-oci-build-preflight/v1"
DEFAULT_RETENTION_HOURS = 24
DEFAULT_BUILDER_NAME = "plastic-promise-oci"
RETENTION_LABEL = "com.plastic-promise.retention"
RETENTION_PROTECT_VALUE = "protect"
_MANAGED_REPOSITORY_PREFIXES = (
    "plastic-promise-local-edge",
    "plastic-promise-server",
    "plastic-promise-compute-node",
    "plastic-promise-inference-node",
    "plastic-promise-local-inference-node",
    "ghcr.io/aldaisuki/plastic-promise-local-edge",
    "ghcr.io/aldaisuki/plastic-promise-server",
    "ghcr.io/aldaisuki/plastic-promise-compute-node",
    "ghcr.io/aldaisuki/plastic-promise-inference-node",
    "ghcr.io/aldaisuki/plastic-promise-local-inference-node",
)
_MANAGED_TITLES = frozenset(
    {
        "Plastic Promise local edge",
        "Plastic Promise MCP runtime",
        "Plastic Promise server backend",
        "Plastic Promise compute node",
        "Plastic Promise local inference node",
    }
)


@dataclass(frozen=True)
class RemovableImage:
    """A project-owned image proven safe for this bounded cleanup."""

    image_id: str
    repository: str
    created_at: str


@dataclass(frozen=True)
class CleanupPlan:
    """Read-only cleanup decision, suitable for audit before mutation."""

    retention_hours: int
    removable_images: tuple[RemovableImage, ...]
    protected_image_ids: dict[str, str]
    retained_image_ids: dict[str, str]


class DockerCleanupError(RuntimeError):
    """Raised when safe Docker inspection or the bounded cleanup cannot finish."""


DockerRunner = Callable[[list[str]], str]


def build_cleanup_plan(
    images: Iterable[Mapping[str, object]],
    *,
    container_image_ids: set[str],
    now: datetime,
    retention_hours: int,
) -> CleanupPlan:
    """Select only stale, unreferenced, unprotected Plastic Promise images."""

    if not 1 <= retention_hours <= 24 * 90:
        raise ValueError("oci_cleanup_retention_hours_invalid")
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=retention_hours)
    removable: list[RemovableImage] = []
    protected: dict[str, str] = {}
    retained: dict[str, str] = {}
    for raw in images:
        image_id = str(raw.get("id") or "").strip()
        labels = raw.get("labels")
        if not image_id:
            continue
        if not isinstance(labels, Mapping):
            protected[image_id] = "image_labels_unreadable"
            continue
        repositories = _image_repositories(raw)
        if not _is_managed_image(repositories, labels):
            retained[image_id] = "outside_project_scope"
            continue
        if image_id in container_image_ids:
            protected[image_id] = "container_reference"
            continue
        if _is_protected(labels):
            protected[image_id] = "retention_label"
            continue
        created_at = _parse_timestamp(raw.get("created_at"))
        if created_at is None:
            protected[image_id] = "creation_time_unreadable"
            continue
        if created_at > cutoff:
            retained[image_id] = "within_retention_window"
            continue
        removable.append(
            RemovableImage(
                image_id=image_id,
                repository=_preferred_repository(repositories),
                created_at=created_at.isoformat(),
            )
        )
    return CleanupPlan(
        retention_hours=retention_hours,
        removable_images=tuple(sorted(removable, key=lambda item: item.image_id)),
        protected_image_ids=dict(sorted(protected.items())),
        retained_image_ids=dict(sorted(retained.items())),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: DockerRunner | None = None,
    output: TextIO | None = None,
    now: datetime | None = None,
) -> int:
    """Inspect or execute the bounded cleanup. Returns a process-style status."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    stream = output or sys.stdout
    command_runner = _docker_runner_with_config(runner or _run_docker, args.docker_config)
    current_time = now or datetime.now(timezone.utc)
    try:
        images = _inspect_images(command_runner)
        container_image_ids = _inspect_container_image_ids(command_runner)
        plan = build_cleanup_plan(
            images,
            container_image_ids=container_image_ids,
            now=current_time,
            retention_hours=args.retention_hours,
        )
        plan_report = _report(plan, builder=args.builder)
        if args.execute:
            # Persist the exact deletion boundary before the first mutable Docker
            # command. ``--report`` is JSON Lines so the terminal outcome is
            # retained alongside the pre-action plan instead of replacing it.
            _write_report(plan_report, stream=stream, report_path=args.report)
            report = dict(plan_report)
            for image in plan.removable_images:
                # A managed image can legitimately carry both the compute-node
                # and local-inference-node tags.  The plan has already proven
                # that every tag is project-owned and that no container
                # references this image, so force is required only to remove
                # the multi-repository tag set as one bounded operation.
                command_runner(["docker", "image", "rm", "--force", image.image_id])
            command_runner(
                [
                    "docker",
                    "buildx",
                    "prune",
                    "--builder",
                    args.builder,
                    "--force",
                    "--filter",
                    f"until={args.retention_hours}h",
                ]
            )
            report["status"] = "cleaned"
            report["event"] = "cleanup_result"
            report["removed_image_ids"] = [image.image_id for image in plan.removable_images]
            report["cache_prune"] = "completed"
            _write_report(report, stream=stream, report_path=args.report)
        else:
            _write_report(plan_report, stream=stream, report_path=args.report)
    except (DockerCleanupError, ValueError) as exc:
        _write_report(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "failed",
                "reason": _reason_code(exc),
            },
            stream=stream,
            report_path=args.report,
        )
        return 2
    except OSError:
        _write_report(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "failed",
                "reason": "oci_cleanup_docker_command_failed",
            },
            stream=stream,
            report_path=args.report,
        )
        return 2
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="remove only eligible project images and Docker's unused buildx cache",
    )
    parser.add_argument(
        "--retention-hours",
        type=int,
        default=DEFAULT_RETENTION_HOURS,
        help=f"retain recent project images and cache entries (default: {DEFAULT_RETENTION_HOURS})",
    )
    parser.add_argument(
        "--builder",
        type=_parse_builder_name,
        default=DEFAULT_BUILDER_NAME,
        help=(
            f"dedicated Buildx builder whose cache may be pruned (default: {DEFAULT_BUILDER_NAME})"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="optional JSON Lines audit report path outside canonical/runtime state",
    )
    parser.add_argument(
        "--docker-config",
        type=_parse_docker_config_directory,
        help="optional absolute Docker config directory passed explicitly to every command",
    )
    return parser


def _parse_builder_name(value: str) -> str:
    """Accept only a named Plastic Promise builder, never Docker's default."""

    builder = value.strip()
    if not builder.startswith("plastic-promise-"):
        raise argparse.ArgumentTypeError("oci_cleanup_builder_name_invalid")
    return builder


def _parse_docker_config_directory(value: str) -> Path:
    """Accept only an explicit absolute directory, never an inherited relative path."""

    directory = Path(value)
    if not directory.is_absolute() or ".." in directory.parts:
        raise argparse.ArgumentTypeError("oci_cleanup_docker_config_invalid")
    return directory


def _docker_runner_with_config(
    runner: DockerRunner,
    docker_config: Path | None,
) -> DockerRunner:
    if docker_config is None:
        return runner

    def configured_runner(command: list[str]) -> str:
        if not command or command[0] != "docker":
            raise DockerCleanupError("oci_cleanup_docker_command_invalid")
        return runner(["docker", "--config", str(docker_config), *command[1:]])

    return configured_runner


def _inspect_images(runner: DockerRunner) -> list[dict[str, object]]:
    raw_rows = runner(["docker", "image", "ls", "--all", "--no-trunc", "--format", "{{json .}}"])
    repositories: dict[str, set[str]] = {}
    for line in raw_rows.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DockerCleanupError("oci_cleanup_image_list_invalid") from exc
        image_id = str(row.get("ID") or "").strip()
        repository = str(row.get("Repository") or "").strip()
        if image_id:
            repositories.setdefault(image_id, set()).add(repository)

    images: list[dict[str, object]] = []
    for image_id, names in sorted(repositories.items()):
        payload = _json_list(runner(["docker", "image", "inspect", image_id]))
        if len(payload) != 1 or not isinstance(payload[0], Mapping):
            raise DockerCleanupError("oci_cleanup_image_inspect_invalid")
        inspection = payload[0]
        config = inspection.get("Config")
        raw_labels = config.get("Labels") if isinstance(config, Mapping) else None
        if raw_labels is None:
            # Docker emits JSON null when an image has no labels. Preserve that
            # unreadable state so the cleanup planner fails closed instead of
            # treating missing metadata as an eligible image.
            labels: object = None
        elif isinstance(raw_labels, Mapping):
            labels = {
                str(key): str(value)
                for key, value in raw_labels.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        else:
            labels = raw_labels
        images.append(
            {
                "id": str(inspection.get("Id") or image_id),
                "repository": _preferred_repository(names),
                "repositories": sorted(names),
                "created_at": inspection.get("Created"),
                "labels": labels,
            }
        )
    return images


def _inspect_container_image_ids(runner: DockerRunner) -> set[str]:
    raw_ids = runner(["docker", "container", "ls", "-aq"])
    image_ids: set[str] = set()
    for container_id in raw_ids.split():
        image_id = runner(
            ["docker", "container", "inspect", "--format", "{{.Image}}", container_id]
        )
        normalized = image_id.strip()
        if normalized:
            image_ids.add(normalized)
    return image_ids


def _run_docker(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DockerCleanupError("oci_cleanup_docker_command_failed") from exc
    return completed.stdout


def _json_list(value: str) -> list[object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DockerCleanupError("oci_cleanup_image_inspect_invalid") from exc
    if not isinstance(parsed, list):
        raise DockerCleanupError("oci_cleanup_image_inspect_invalid")
    return parsed


def _image_repositories(raw: Mapping[str, object]) -> set[str]:
    """Return every known repository tag for an image, not just one display name."""

    declared = raw.get("repositories")
    if isinstance(declared, (list, tuple, set, frozenset)):
        names = {str(value).strip() for value in declared if str(value).strip()}
    else:
        repository = str(raw.get("repository") or "").strip()
        names = {repository} if repository else set()
    return names or {"<none>"}


def _preferred_repository(names: Iterable[str]) -> str:
    candidates = sorted(name for name in names if name and name != "<none>")
    return candidates[0] if candidates else "<none>"


def _is_managed_image(repositories: set[str], labels: Mapping[object, object]) -> bool:
    """Require all tags to be project-owned before any image removal is possible.

    Docker image IDs can have multiple tags. A Plastic Promise tag alongside an
    unrelated tag is intentionally retained: removing the image ID would also
    remove the other project's tag. Untagged images rely on the OCI title
    label, which is the only safe project identity available in that case.
    """

    tagged_repositories = {
        repository for repository in repositories if repository and repository != "<none>"
    }
    if tagged_repositories:
        return all(_is_managed_repository(repository) for repository in tagged_repositories)
    return str(labels.get("org.opencontainers.image.title") or "") in _MANAGED_TITLES


def _is_managed_repository(repository: str) -> bool:
    return any(
        repository == prefix or repository.startswith(prefix + "/")
        for prefix in _MANAGED_REPOSITORY_PREFIXES
    )


def _is_protected(labels: Mapping[object, object]) -> bool:
    return str(labels.get(RETENTION_LABEL) or "").strip().casefold() in {
        "protect",
        "true",
        "1",
    }


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _report(plan: CleanupPlan, *, builder: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "pre_action_plan",
        "status": "planned",
        "retention_hours": plan.retention_hours,
        "builder": builder,
        "would_remove_images": [asdict(image) for image in plan.removable_images],
        "protected_image_ids": plan.protected_image_ids,
        "retained_image_ids": plan.retained_image_ids,
        "cache_prune": "would_run",
    }


def _write_report(report: dict[str, object], *, stream: TextIO, report_path: Path | None) -> None:
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    stream.write(encoded + "\n")
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("a", encoding="utf-8") as report_file:
            report_file.write(encoded + "\n")


def _reason_code(exc: Exception) -> str:
    message = str(exc).strip()
    return message if message.startswith("oci_cleanup_") else "oci_cleanup_failed"


if __name__ == "__main__":  # pragma: no cover - CLI entry point.
    raise SystemExit(main())
