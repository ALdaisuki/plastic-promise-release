"""One-click local compute-node build orchestration (bootstrap phase).

This module plans and (optionally) executes the platform one-click build
script.  It is deliberately a local derived-inference preflight: it never
pushes an image, publishes an artifact, starts MCP, or opens canonical
SQLite/LanceDB state.  The build script itself is the single source of
truth for the bounded cleanup, resource gate, and GPU smoke.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .manifest import DeploymentContractError

_PLATFORM_SCRIPTS = {
    "Windows": "scripts/build_compute_node.ps1",
    "Darwin": "scripts/build_compute_node.sh",
    "Linux": "scripts/build_compute_node.sh",
}
_SOURCE_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class NodeBuildPlan:
    """A fully resolved, executable one-click compute-node build plan."""

    platform_name: str
    script: Path
    arguments: tuple[str, ...]
    source_revision: str
    variant: str
    dry_run: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "plastic-promise/deployment-node-build/v1",
            "platform": self.platform_name,
            "script": str(self.script),
            "command": [str(self.script), *list(self.arguments)],
            "source_revision": self.source_revision,
            "variant": self.variant,
            "dry_run": self.dry_run,
        }


def _repository_root() -> Path:
    """Resolve the source checkout owning the deployment package and scripts."""

    candidate = Path(__file__).resolve().parents[2]
    if not (candidate / "scripts").is_dir():
        raise DeploymentContractError("build_node_source_checkout_required")
    return candidate


def _resolve_source_revision(root: Path, explicit: str | None) -> str:
    if explicit is not None:
        revision = explicit.strip().lower()
        if _SOURCE_REVISION_RE.fullmatch(revision) is None:
            raise DeploymentContractError("build_node_source_revision_invalid")
        return revision
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise DeploymentContractError("build_node_git_unavailable") from exc
    revision = result.stdout.strip().lower() if result.returncode == 0 else ""
    if _SOURCE_REVISION_RE.fullmatch(revision) is None:
        raise DeploymentContractError("build_node_source_revision_required")
    return revision


def _detect_variant(requested: str, root: Path) -> str:
    if requested in {"cpu", "cuda"}:
        return requested
    if shutil.which("nvidia-smi") is not None:
        return "cuda"
    if (root / "deploy" / "local-inference-node" / "compose.cuda.yaml").exists() and (
        root / ".nvidia" / "gpus"
    ).exists():
        return "cuda"
    return "cpu"


def _platform_script(platform_name: str, root: Path) -> Path:
    relative = _PLATFORM_SCRIPTS.get(platform_name)
    if relative is None:
        raise DeploymentContractError("build_node_platform_unsupported")
    script = root / relative
    if not script.is_file():
        raise DeploymentContractError("build_node_script_missing")
    return script


def plan_node_build(
    *,
    source_revision: str | None = None,
    variant: str = "auto",
    builder: str | None = None,
    image_tag: str | None = None,
    retention_hours: int | None = None,
    report_directory: str | None = None,
    node_config: Path | None = None,
    runtime_status: Path | None = None,
    skip_gpu_smoke: bool = False,
    no_start: bool = False,
    dry_run: bool = False,
    repository_root: Path | None = None,
) -> NodeBuildPlan:
    """Resolve the exact platform script invocation with no side effects."""

    root = repository_root or _repository_root()
    platform_name = platform.system()
    script = _platform_script(platform_name, root)
    revision = _resolve_source_revision(root, source_revision)
    resolved_variant = _detect_variant(variant, root)
    arguments = [
        "--source-revision",
        revision,
        "--variant",
        resolved_variant,
    ]
    if builder is not None:
        arguments += ["--builder", builder]
    if image_tag is not None:
        arguments += ["--image-tag", image_tag]
    if retention_hours is not None:
        arguments += ["--retention-hours", str(retention_hours)]
    if report_directory is not None:
        arguments += ["--report-directory", report_directory]
    if node_config is not None:
        arguments += ["--node-config", str(node_config)]
    if runtime_status is not None:
        arguments += ["--runtime-status", str(runtime_status)]
    if skip_gpu_smoke:
        arguments.append("--skip-gpu-smoke")
    if no_start:
        arguments.append("--no-start")
    return NodeBuildPlan(
        platform_name=platform_name,
        script=script,
        arguments=tuple(arguments),
        source_revision=revision,
        variant=resolved_variant,
        dry_run=dry_run,
    )


def _script_command(plan: NodeBuildPlan) -> list[str]:
    """Resolve the platform interpreter for the one-click build script."""

    if plan.script.suffix == ".ps1":
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(plan.script),
        ]
    if plan.script.suffix == ".sh":
        return ["bash", str(plan.script)]
    raise DeploymentContractError("build_node_script_interpreter_unsupported")


def run_node_build(plan: NodeBuildPlan) -> int:
    """Execute the resolved platform one-click build script."""

    return subprocess.call(
        [*_script_command(plan), *plan.arguments],
        cwd=plan.script.parents[1],
    )
