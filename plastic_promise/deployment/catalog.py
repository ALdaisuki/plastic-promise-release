"""Deployment profile and module vocabulary.

This module intentionally contains no runtime, storage, provider, or control-plane
imports.  It is the stable contract consumed by deployment manifests and release
metadata; service-specific behaviour belongs to later deployment slices.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeploymentProfile:
    """One supported, end-user deployment topology."""

    id: str
    topology: str
    base_modules: tuple[str, ...]
    scheduling_default: str
    resource_policy: ResourcePolicy


@dataclass(frozen=True)
class ResourcePolicy:
    """Planning metadata consumed by a later, side-effect-free preflight."""

    minimum_free_bytes: int
    minimum_free_fraction: float
    state_hosts: tuple[str, ...]
    model_artifacts_bundled: bool


_POST_APPLY_RESOURCE_POLICY = ResourcePolicy(
    minimum_free_bytes=10 * 1024**3,
    minimum_free_fraction=0.2,
    state_hosts=("canonical-sqlite", "lancedb-shadow-rebuild", "online-backup"),
    model_artifacts_bundled=False,
)


@dataclass(frozen=True)
class DeploymentModule:
    """A deployable capability and its profile and risk constraints."""

    id: str
    risk_tier: str
    supported_profiles: tuple[str, ...]
    requires: tuple[str, ...] = ()


_STABLE_PROFILES: tuple[DeploymentProfile, ...] = (
    DeploymentProfile(
        id="local-all-in-one",
        topology="single-host",
        base_modules=(
            "canonical-runtime",
            "durable-outbox",
            "derived-index",
            "operator-dashboard",
        ),
        # Inference is owned by the registered compute node. Ollama is only
        # an explicit legacy compatibility module, never a profile default.
        scheduling_default="remote-node-first",
        resource_policy=_POST_APPLY_RESOURCE_POLICY,
    ),
    DeploymentProfile(
        id="local-cloud",
        topology="single-host-cloud-inference",
        base_modules=(
            "canonical-runtime",
            "durable-outbox",
            "derived-index",
            "operator-dashboard",
            "cloud-inference",
        ),
        scheduling_default="cloud-only",
        resource_policy=_POST_APPLY_RESOURCE_POLICY,
    ),
    DeploymentProfile(
        id="split-accelerated",
        topology="server-local-inference-node",
        base_modules=(
            "canonical-runtime",
            "durable-outbox",
            "derived-index",
            "operator-dashboard",
            "heterogeneous-inference-node",
        ),
        scheduling_default="remote-node-first",
        resource_policy=_POST_APPLY_RESOURCE_POLICY,
    ),
)

_ALL_PROFILE_IDS = tuple(profile.id for profile in _STABLE_PROFILES)
_MODULES: tuple[DeploymentModule, ...] = (
    DeploymentModule("canonical-runtime", "core", _ALL_PROFILE_IDS),
    DeploymentModule(
        "durable-outbox",
        "core",
        _ALL_PROFILE_IDS,
        requires=("canonical-runtime",),
    ),
    DeploymentModule(
        "derived-index",
        "core",
        _ALL_PROFILE_IDS,
        requires=("durable-outbox",),
    ),
    DeploymentModule(
        "operator-dashboard",
        "core",
        _ALL_PROFILE_IDS,
        requires=("canonical-runtime",),
    ),
    DeploymentModule(
        "cloud-inference",
        "optional",
        ("local-all-in-one", "local-cloud"),
    ),
    DeploymentModule(
        "local-ollama",
        "optional",
        ("local-all-in-one",),
    ),
    DeploymentModule(
        "heterogeneous-inference-node",
        "optional",
        ("split-accelerated",),
        requires=("durable-outbox",),
    ),
    DeploymentModule(
        "accelerator-max",
        "high-risk",
        _ALL_PROFILE_IDS,
        requires=("durable-outbox", "derived-index"),
    ),
    DeploymentModule(
        "maintenance-daemon",
        "high-risk",
        _ALL_PROFILE_IDS,
        requires=("durable-outbox",),
    ),
)


def stable_profiles() -> tuple[DeploymentProfile, ...]:
    """Return the release-supported profiles in stable display order."""

    return _STABLE_PROFILES


def stable_profile_ids() -> tuple[str, ...]:
    """Return the identifiers accepted by the first stable release contract."""

    return tuple(profile.id for profile in stable_profiles())


def profile_by_id(profile_id: str) -> DeploymentProfile | None:
    """Find a supported profile without coupling callers to catalog storage."""

    return next((profile for profile in stable_profiles() if profile.id == profile_id), None)


def module_by_id(module_id: str) -> DeploymentModule | None:
    """Find a module definition from the deployment-owned capability catalog."""

    return next((module for module in _MODULES if module.id == module_id), None)


def deployment_modules() -> tuple[DeploymentModule, ...]:
    """Return modules in the stable dependency-plan ordering."""

    return _MODULES
