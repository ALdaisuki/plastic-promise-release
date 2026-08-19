"""Secret-free deployment-manifest parsing and profile resolution."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import deployment_modules, module_by_id, profile_by_id

DEPLOYMENT_MANIFEST_SCHEMA_VERSION = "plastic-promise-deployment/v1"
_DEPLOYMENT_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_MANIFEST_FIELDS = {
    "schema_version",
    "deployment_id",
    "profile",
    "modules",
    "nodes",
    "resource_budget",
    "resource_locations",
}
_MODULE_SELECTION_FIELDS = {"enabled", "acknowledge_high_risk"}
_NODE_FIELDS = {"id", "role", "ssh_host", "capabilities", "max_concurrency"}
_NODE_REQUIRED_FIELDS = {"id", "role", "ssh_host", "capabilities"}
_NODE_CAPABILITY_FIELDS = {"embedding", "rerank"}
_NODE_MAX_CONCURRENCY = 64
_RESOURCE_BUDGET_FIELDS = {
    "image_layers_bytes",
    "image_unpack_bytes",
    "model_cache_bytes",
    "lancedb_shadow_rebuild_bytes",
    "rollback_coexistence_bytes",
}
_RESOURCE_LOCATION_FIELDS = {"container_store", "model_cache"}
_SECRET_FIELD_TOKENS = {
    "apikey",
    "authorization",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


@dataclass(frozen=True)
class ResolvedDeployment:
    """The non-secret, deterministic profile plan for a deployment."""

    deployment_id: str
    profile_id: str
    module_ids: tuple[str, ...]
    node_ids: tuple[str, ...]
    nodes: tuple[DeploymentNode, ...]
    resource_budget: ResourceBudget | None
    resource_locations: ResourceLocations | None


@dataclass(frozen=True)
class DeploymentNode:
    """A non-secret, declared local inference node contract."""

    id: str
    role: str
    ssh_host: str
    capabilities: tuple[str, ...]
    max_concurrency: int


@dataclass(frozen=True)
class ResourceBudget:
    """Measured write estimates for the selected deployment profile.

    Existing Docker and model-cache occupancy is deliberately absent: preflight
    observes it from the selected local paths rather than trusting a stale
    manifest value.
    """

    image_layers_bytes: int
    image_unpack_bytes: int
    model_cache_bytes: int
    lancedb_shadow_rebuild_bytes: int
    rollback_coexistence_bytes: int

    @property
    def planned_write_bytes(self) -> int:
        return (
            self.image_layers_bytes
            + self.image_unpack_bytes
            + self.model_cache_bytes
            + self.lancedb_shadow_rebuild_bytes
            + self.rollback_coexistence_bytes
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "image_layers_bytes": self.image_layers_bytes,
            "image_unpack_bytes": self.image_unpack_bytes,
            "model_cache_bytes": self.model_cache_bytes,
            "lancedb_shadow_rebuild_bytes": self.lancedb_shadow_rebuild_bytes,
            "rollback_coexistence_bytes": self.rollback_coexistence_bytes,
            "planned_write_bytes": self.planned_write_bytes,
        }


@dataclass(frozen=True)
class ResourceLocations:
    """Non-secret local roots used to measure the selected runtime footprint."""

    container_store: Path | None
    model_cache: Path | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "container_store": str(self.container_store) if self.container_store else None,
            "model_cache": str(self.model_cache) if self.model_cache else None,
        }


class DeploymentContractError(ValueError):
    """Raised when a deployment contract cannot be safely resolved."""


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeploymentContractError(f"manifest_mapping_required:{field}")
    return value


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DeploymentContractError(f"manifest_string_required:{field}")
    return value


def _require_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise DeploymentContractError(f"manifest_list_required:{field}")
    return value


def _resource_budget(value: object | None) -> ResourceBudget | None:
    """Parse complete non-secret capacity evidence without guessing values."""

    if value is None:
        return None
    budget = _require_mapping(value, "resource_budget")
    unknown_fields = sorted(set(budget) - _RESOURCE_BUDGET_FIELDS)
    if unknown_fields:
        raise DeploymentContractError(f"resource_budget_unknown_field:{unknown_fields[0]}")
    missing_fields = sorted(_RESOURCE_BUDGET_FIELDS - set(budget))
    if missing_fields:
        raise DeploymentContractError(f"resource_budget_field_required:{missing_fields[0]}")
    values: dict[str, int] = {}
    for field in sorted(_RESOURCE_BUDGET_FIELDS):
        raw = budget[field]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise DeploymentContractError(f"resource_budget_bytes_invalid:{field}")
        values[field] = raw
    return ResourceBudget(**values)


def _resource_locations(value: object | None) -> ResourceLocations | None:
    """Parse non-secret local paths without inventing runtime-specific defaults."""

    if value is None:
        return None
    locations = _require_mapping(value, "resource_locations")
    unknown_fields = sorted(set(locations) - _RESOURCE_LOCATION_FIELDS)
    if unknown_fields:
        raise DeploymentContractError(f"resource_locations_unknown_field:{unknown_fields[0]}")
    values: dict[str, Path | None] = {}
    for field in sorted(_RESOURCE_LOCATION_FIELDS):
        raw = locations.get(field)
        if raw is None:
            values[field] = None
            continue
        if not isinstance(raw, str) or not raw.strip():
            raise DeploymentContractError(f"resource_locations_path_invalid:{field}")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise DeploymentContractError(f"resource_locations_path_absolute_required:{field}")
        values[field] = path
    return ResourceLocations(**values)


def _validate_resource_budget_for_selection(
    resource_budget: ResourceBudget | None,
    resource_locations: ResourceLocations | None,
    *,
    profile_id: str,
    module_ids: list[str],
) -> None:
    """Reject placeholder capacity numbers for components that will write data."""

    if resource_budget is None:
        return
    required_fields = {"lancedb_shadow_rebuild_bytes", "rollback_coexistence_bytes"}
    if profile_id == "split-accelerated":
        required_fields.update({"image_layers_bytes", "image_unpack_bytes"})
    if {"local-ollama", "heterogeneous-inference-node"} & set(module_ids):
        required_fields.add("model_cache_bytes")
    for field in sorted(required_fields):
        if getattr(resource_budget, field) <= 0:
            raise DeploymentContractError(f"resource_budget_estimate_required:{field}")
    if resource_budget.image_layers_bytes + resource_budget.image_unpack_bytes > 0 and (
        resource_locations is None or resource_locations.container_store is None
    ):
        raise DeploymentContractError("resource_locations_container_store_required")
    if resource_budget.model_cache_bytes > 0 and (
        resource_locations is None or resource_locations.model_cache is None
    ):
        raise DeploymentContractError("resource_locations_model_cache_required")


def _scan_for_secrets(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise DeploymentContractError("manifest_string_key_required")
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in _SECRET_FIELD_TOKENS:
                raise DeploymentContractError(f"secret_field_forbidden:{key}")
            _scan_for_secrets(nested)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _scan_for_secrets(item)
        return
    if isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise DeploymentContractError("secret_value_detected")


def resolve_deployment_manifest(payload: Mapping[str, object]) -> ResolvedDeployment:
    """Validate a non-secret manifest and resolve its profile baseline."""

    manifest = _require_mapping(payload, "manifest")
    _scan_for_secrets(manifest)
    unknown_fields = sorted(set(manifest) - _MANIFEST_FIELDS)
    if unknown_fields:
        raise DeploymentContractError(f"manifest_unknown_field:{unknown_fields[0]}")

    schema_version = _require_string(manifest.get("schema_version"), "schema_version")
    if schema_version != DEPLOYMENT_MANIFEST_SCHEMA_VERSION:
        raise DeploymentContractError("manifest_schema_version_unsupported")

    deployment_id = _require_string(manifest.get("deployment_id"), "deployment_id")
    if not _DEPLOYMENT_ID.fullmatch(deployment_id):
        raise DeploymentContractError("manifest_deployment_id_invalid")

    profile_id = _require_string(manifest.get("profile"), "profile")
    profile = profile_by_id(profile_id)
    if profile is None:
        raise DeploymentContractError("manifest_profile_unsupported")
    resource_budget = _resource_budget(manifest.get("resource_budget"))
    resource_locations = _resource_locations(manifest.get("resource_locations"))

    nodes = _require_list(manifest.get("nodes", []), "nodes")
    node_ids: list[str] = []
    node_declarations: list[DeploymentNode] = []
    for node in nodes:
        declaration = _require_mapping(node, "nodes.item")
        if set(declaration) - _NODE_FIELDS or _NODE_REQUIRED_FIELDS - set(declaration):
            raise DeploymentContractError("manifest_node_contract_invalid")
        node_id = _require_string(declaration.get("id"), "nodes.item.id")
        if _DEPLOYMENT_ID.fullmatch(node_id) is None:
            raise DeploymentContractError("manifest_node_id_invalid")
        if declaration.get("role") != "local-heterogeneous-inference-node":
            raise DeploymentContractError("manifest_node_role_invalid")
        ssh_host = _require_string(declaration.get("ssh_host"), "nodes.item.ssh_host")
        if any(character.isspace() for character in ssh_host):
            raise DeploymentContractError("manifest_node_ssh_host_invalid")
        capabilities = _require_mapping(declaration.get("capabilities"), "nodes.item.capabilities")
        if set(capabilities) != _NODE_CAPABILITY_FIELDS or any(
            value is not True for value in capabilities.values()
        ):
            raise DeploymentContractError("manifest_node_capabilities_invalid")
        max_concurrency = declaration.get("max_concurrency", 1)
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or not 1 <= max_concurrency <= _NODE_MAX_CONCURRENCY
        ):
            raise DeploymentContractError("manifest_node_max_concurrency_invalid")
        if node_id in node_ids:
            raise DeploymentContractError("manifest_node_id_duplicate")
        node_ids.append(node_id)
        node_declarations.append(
            DeploymentNode(
                id=node_id,
                role="local-heterogeneous-inference-node",
                ssh_host=ssh_host,
                capabilities=tuple(sorted(_NODE_CAPABILITY_FIELDS)),
                max_concurrency=max_concurrency,
            )
        )
    if profile.id == "split-accelerated" and len(node_ids) != 1:
        raise DeploymentContractError("manifest_inference_node_required")
    if profile.id != "split-accelerated" and node_ids:
        raise DeploymentContractError("manifest_nodes_profile_incompatible")

    modules = _require_mapping(manifest.get("modules"), "modules")
    for module_id in modules:
        if not isinstance(module_id, str):
            raise DeploymentContractError("manifest_module_id_invalid")
        if module_by_id(module_id) is None:
            raise DeploymentContractError("manifest_module_unsupported")

    selected_modules = list(profile.base_modules)
    for module in deployment_modules():
        if module.id not in modules:
            continue
        module_id = module.id
        value = modules[module_id]
        if module.risk_tier == "core":
            raise DeploymentContractError("manifest_core_module_implicit")
        if profile.id not in module.supported_profiles:
            raise DeploymentContractError("manifest_module_profile_incompatible")
        selection = _require_mapping(value, f"modules.{module_id}")
        unknown_selection_fields = sorted(set(selection) - _MODULE_SELECTION_FIELDS)
        if unknown_selection_fields:
            raise DeploymentContractError(
                f"manifest_module_unknown_field:{module_id}:{unknown_selection_fields[0]}"
            )
        if selection.get("enabled") is not True:
            raise DeploymentContractError("manifest_module_enabled_must_be_true")
        if module.risk_tier == "high-risk" and selection.get("acknowledge_high_risk") is not True:
            raise DeploymentContractError("high_risk_module_acknowledgement_required")
        if module.risk_tier != "high-risk" and "acknowledge_high_risk" in selection:
            raise DeploymentContractError("manifest_high_risk_acknowledgement_unexpected")
        for requirement in module.requires:
            if requirement not in selected_modules:
                selected_modules.append(requirement)
        if module.id not in selected_modules:
            selected_modules.append(module.id)

    _validate_resource_budget_for_selection(
        resource_budget,
        resource_locations,
        profile_id=profile.id,
        module_ids=selected_modules,
    )

    return ResolvedDeployment(
        deployment_id=deployment_id,
        profile_id=profile.id,
        module_ids=tuple(selected_modules),
        node_ids=tuple(node_ids),
        nodes=tuple(node_declarations),
        resource_budget=resource_budget,
        resource_locations=resource_locations,
    )


def load_deployment_manifest(path: Path) -> ResolvedDeployment:
    """Load a JSON deployment manifest through the file-system public seam."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentContractError("manifest_json_unreadable") from exc
    return resolve_deployment_manifest(_require_mapping(payload, "manifest"))
