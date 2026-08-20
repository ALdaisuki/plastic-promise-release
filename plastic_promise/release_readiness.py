"""Read-only release readiness checks for deployment profile artifacts.

This module is deliberately narrower than the deployment controller.  It
proves that a checked-in release profile can be parsed, planned, preflighted,
and rendered into platform activation recipes without creating a state root or
starting anything.  It is not an installer, migration executor, or release
publisher.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from plastic_promise.deployment.controller import DeploymentController
from plastic_promise.deployment.manifest import load_deployment_manifest
from plastic_promise.deployment.plan import create_deployment_plan
from plastic_promise.deployment.runtime_assets import runtime_assets_for_module

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


RELEASE_DEPLOYMENT_VERIFICATION_SCHEMA_VERSION = "plastic-promise/release-deployment-verify/v1"
_RENDER_PLATFORMS = ("Linux", "Darwin", "Windows")


class ReleaseReadinessError(ValueError):
    """Stable refusal from the source-only release verification interface."""


@dataclass(frozen=True)
class ReleaseDeploymentVerification:
    """A non-sensitive, zero-side-effect proof for one profile manifest."""

    profile_id: str
    plan_hash: str
    module_ids: tuple[str, ...]
    preflight: Mapping[str, object]
    runtime_assets: Mapping[str, Mapping[str, tuple[str, ...]]]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": RELEASE_DEPLOYMENT_VERIFICATION_SCHEMA_VERSION,
            "source_only": True,
            "profile": self.profile_id,
            "plan_hash": self.plan_hash,
            "modules": list(self.module_ids),
            "preflight": dict(self.preflight),
            "runtime_assets": {
                module_id: {
                    platform_name: list(asset_names)
                    for platform_name, asset_names in platform_assets.items()
                }
                for module_id, platform_assets in self.runtime_assets.items()
            },
        }


def verify_source_only_deployment(
    *,
    manifest_path: Path,
    state_root: Path,
    controller: DeploymentController | None = None,
) -> ReleaseDeploymentVerification:
    """Verify a profile without creating runtime state or activating services.

    The absent state root is an explicit part of the interface: a caller cannot
    accidentally use this release-check path to inspect or mutate an existing
    installation.  Runtime asset rendering is in-memory only and returns file
    names, never paths or asset contents.
    """

    resolved_state_root = state_root.expanduser().resolve(strict=False)
    if resolved_state_root.exists():
        raise ReleaseReadinessError("release_verify_state_root_must_be_absent")

    resolved = load_deployment_manifest(manifest_path)
    plan = create_deployment_plan(resolved, state_root=resolved_state_root, operation="install")
    selected_controller = controller or DeploymentController()
    preflight = selected_controller.preflight(plan)
    if resolved_state_root.exists():
        raise ReleaseReadinessError("release_verify_state_root_mutated")
    if not preflight.ok:
        raise ReleaseReadinessError("release_verify_preflight_failed")

    rendered_assets: dict[str, dict[str, tuple[str, ...]]] = {}
    for module_id in plan.module_ids:
        platform_assets = {
            platform_name: tuple(
                asset.relative_path.as_posix()
                for asset in runtime_assets_for_module(
                    plan,
                    module_id,
                    system_name=platform_name,
                )
            )
            for platform_name in _RENDER_PLATFORMS
        }
        if any(platform_assets.values()):
            rendered_assets[module_id] = platform_assets

    if resolved_state_root.exists():
        raise ReleaseReadinessError("release_verify_state_root_mutated")

    return ReleaseDeploymentVerification(
        profile_id=plan.profile_id,
        plan_hash=plan.plan_hash,
        module_ids=plan.module_ids,
        preflight=preflight.as_dict(),
        runtime_assets=rendered_assets,
    )
