"""Closed role contracts shared by topology and artifact compilation.

This standard-library-only module is the single repository authority for the
three endpoint roles.  It owns what each role may do, what an OCI artifact may
claim, which capability contracts it may expose, and which source paths may
cross the role-package seam.  Runtime admission, role-package materialisation,
and OCI planning consume this interface rather than maintaining parallel
matrices.

The local-edge static compiler is intentionally here as well: the asset set is
part of the role's source surface, not a generic Dashboard concern.  It accepts
only the two read-only ppctl operations and fails closed when a new transport,
credential surface, or mutation method appears.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

PP_LOCAL_EDGE = "pp-local-edge"
PP_SERVER_BACKEND = "pp-server-backend"
PP_COMPUTE_NODE = "pp-compute-node"

ENDPOINT_ROLES = (PP_LOCAL_EDGE, PP_SERVER_BACKEND, PP_COMPUTE_NODE)
EDGE_STATIC_SURFACE_SCHEMA_VERSION = "plastic-promise-edge-static-surface/v1"
COMPUTE_PACKAGE_MANIFEST_SCHEMA_VERSION = "plastic-promise-compute-package-manifest/v1"

_EDGE_STATIC_ASSET_PATHS = (
    "deploy/local-edge/static/index.html",
    "deploy/local-edge/static/app.css",
    "deploy/local-edge/static/app.js",
)

_COLLABORATION_FOUNDATION = (
    (
        "plastic_promise.collaboration",
        "plastic_promise/collaboration/__init__.py",
    ),
    (
        "plastic_promise.collaboration.acceptance_receipt",
        "plastic_promise/collaboration/acceptance_receipt.py",
    ),
    (
        "plastic_promise.collaboration.activity_update",
        "plastic_promise/collaboration/activity_update.py",
    ),
    (
        "plastic_promise.collaboration.awareness",
        "plastic_promise/collaboration/awareness.py",
    ),
    (
        "plastic_promise.collaboration.canonical_time",
        "plastic_promise/collaboration/canonical_time.py",
    ),
    (
        "plastic_promise.collaboration.context_projection",
        "plastic_promise/collaboration/context_projection.py",
    ),
    (
        "plastic_promise.collaboration.context_supply_runtime",
        "plastic_promise/collaboration/context_supply_runtime.py",
    ),
    (
        "plastic_promise.collaboration.contracts",
        "plastic_promise/collaboration/contracts.py",
    ),
    (
        "plastic_promise.collaboration.coordination_plan",
        "plastic_promise/collaboration/coordination_plan.py",
    ),
    (
        "plastic_promise.collaboration.coordinator_supervisor",
        "plastic_promise/collaboration/coordinator_supervisor.py",
    ),
    (
        "plastic_promise.collaboration.durable_acceptance_store",
        "plastic_promise/collaboration/durable_acceptance_store.py",
    ),
    (
        "plastic_promise.collaboration.durable_activity_store",
        "plastic_promise/collaboration/durable_activity_store.py",
    ),
    (
        "plastic_promise.collaboration.durable_coordination_plan_store",
        "plastic_promise/collaboration/durable_coordination_plan_store.py",
    ),
    (
        "plastic_promise.collaboration.durable_coordinator_store",
        "plastic_promise/collaboration/durable_coordinator_store.py",
    ),
    (
        "plastic_promise.collaboration.durable_role_store",
        "plastic_promise/collaboration/durable_role_store.py",
    ),
    (
        "plastic_promise.collaboration.durable_runtime",
        "plastic_promise/collaboration/durable_runtime.py",
    ),
    (
        "plastic_promise.collaboration.event_log",
        "plastic_promise/collaboration/event_log.py",
    ),
    (
        "plastic_promise.collaboration.lease_contract",
        "plastic_promise/collaboration/lease_contract.py",
    ),
    (
        "plastic_promise.collaboration.lease_adapters",
        "plastic_promise/collaboration/lease_adapters.py",
    ),
    (
        "plastic_promise.collaboration.passive_bridge",
        "plastic_promise/collaboration/passive_bridge.py",
    ),
    (
        "plastic_promise.collaboration.passive_runtime_adapter",
        "plastic_promise/collaboration/passive_runtime_adapter.py",
    ),
    (
        "plastic_promise.collaboration.policy_binding",
        "plastic_promise/collaboration/policy_binding.py",
    ),
    (
        "plastic_promise.collaboration.role_assignment",
        "plastic_promise/collaboration/role_assignment.py",
    ),
    (
        "plastic_promise.collaboration.runtime_binding",
        "plastic_promise/collaboration/runtime_binding.py",
    ),
)

_ROOT_RUNTIME_FILES = (
    "plastic_promise/__init__.py",
    "plastic_promise/__main__.py",
    "plastic_promise/adaptive_retrieval.py",
    "plastic_promise/behavior.py",
    "plastic_promise/endpoint_roles.py",
    "plastic_promise/issue.py",
    "plastic_promise/pack.py",
    "plastic_promise/py.typed",
    "plastic_promise/release_package_naming.py",
    "plastic_promise/release_manifest.py",
    "plastic_promise/release_readiness.py",
    "plastic_promise/smart_extractor.py",
)

_SERVER_RUNTIME_PACKAGES = (
    "plastic_promise/cli",
    "plastic_promise/client",
    "plastic_promise/control_plane",
    "plastic_promise/core",
    "plastic_promise/cron",
    "plastic_promise/defense",
    "plastic_promise/deployment",
    "plastic_promise/extensions",
    "plastic_promise/growth",
    "plastic_promise/knowledge",
    "plastic_promise/launcher",
    "plastic_promise/loop",
    "plastic_promise/mcp",
    "plastic_promise/memory",
    "plastic_promise/passive_memory",
    "plastic_promise/principles",
    "plastic_promise/reflection",
    "plastic_promise/skills",
)

_SERVER_COMPUTE_SOURCE_EXCLUSIONS = (
    "plastic_promise/core/backend_inference.py",
    "plastic_promise/core/embedder.py",
    "plastic_promise/core/inference_provider.py",
    "plastic_promise/core/inference_jobs.py",
    "plastic_promise/core/provider_http.py",
    "plastic_promise/core/reranker.py",
    "plastic_promise/client/local_rerank_executor.py",
    "plastic_promise/local_inference_node",
    "plastic_promise/mcp/inference_gateway.py",
    "plastic_promise/mcp/inference_gateway_server.py",
)


class EndpointRoleContractError(ValueError):
    """A stable failure raised by the closed endpoint-role interface."""


@dataclass(frozen=True, slots=True)
class ComputeCapabilityContract:
    """One versioned compute contract owned by the compute package."""

    kind: str
    contract_version: str
    input_schema: str
    result_schema: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "contract_version": self.contract_version,
            "input_schema": self.input_schema,
            "result_schema": self.result_schema,
        }


@dataclass(frozen=True, slots=True)
class ComputePackageManifest:
    """Single capability and package-boundary authority for compute execution."""

    capabilities: tuple[ComputeCapabilityContract, ...]
    server_source_exclusions: tuple[str, ...]
    schema_version: str = COMPUTE_PACKAGE_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != COMPUTE_PACKAGE_MANIFEST_SCHEMA_VERSION:
            raise EndpointRoleContractError("compute_package_manifest_schema_unsupported")
        kinds = tuple(item.kind for item in self.capabilities)
        contracts = tuple(item.contract_version for item in self.capabilities)
        if len(kinds) != len(set(kinds)) or len(contracts) != len(set(contracts)):
            raise EndpointRoleContractError("compute_package_manifest_capability_duplicate")
        if len(self.server_source_exclusions) != len(set(self.server_source_exclusions)):
            raise EndpointRoleContractError("compute_package_manifest_exclusion_duplicate")

    @property
    def capability_contracts(self) -> tuple[str, ...]:
        return tuple(item.contract_version for item in self.capabilities)

    @property
    def capability_label(self) -> str:
        return ",".join(self.capability_contracts)

    def capability_for(self, kind: object) -> ComputeCapabilityContract:
        if isinstance(kind, str):
            for capability in self.capabilities:
                if capability.kind == kind:
                    return capability
        raise EndpointRoleContractError("compute_package_manifest_capability_invalid")


_COMPUTE_PACKAGE_MANIFEST = ComputePackageManifest(
    capabilities=(
        ComputeCapabilityContract(
            kind="embedding",
            contract_version="embedding/v1",
            input_schema="embedding-input/v1",
            result_schema="embedding-result/v1",
        ),
        ComputeCapabilityContract(
            kind="rerank",
            contract_version="rerank/v1",
            input_schema="rerank-input/v1",
            result_schema="rerank-result/v1",
        ),
        ComputeCapabilityContract(
            kind="structured-json",
            contract_version="structured-json/v1",
            input_schema="structured-json-input/v1",
            result_schema="structured-json-result/v1",
        ),
    ),
    server_source_exclusions=_SERVER_COMPUTE_SOURCE_EXCLUSIONS,
)


def compute_package_manifest() -> ComputePackageManifest:
    """Return the immutable repository-owned compute package manifest."""

    return _COMPUTE_PACKAGE_MANIFEST


@dataclass(frozen=True, slots=True)
class EndpointRoleContract:
    """Complete deployment and packaging policy for one endpoint role."""

    role: str
    actions: tuple[str, ...]
    descriptive_authorities: tuple[str, ...]
    artifact_authorities: tuple[str, ...]
    capability_contracts: tuple[str, ...]
    package_kind: str
    distribution_name: str
    source_paths: tuple[str, ...]
    source_exclusions: tuple[str, ...] = ()
    collaboration_modules: tuple[str, ...] = ()
    collaboration_source_paths: tuple[str, ...] = ()
    collaboration_writer_surface: str = "absent"
    package_dependencies: tuple[str, ...] = ()
    package_scripts: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.role not in ENDPOINT_ROLES:
            raise EndpointRoleContractError("endpoint_role_contract_role_invalid")
        if self.package_kind not in {"static", "python"}:
            raise EndpointRoleContractError("endpoint_role_contract_package_kind_invalid")
        if not self.distribution_name.startswith("plastic-promise-"):
            raise EndpointRoleContractError("endpoint_role_contract_distribution_invalid")
        tuple_fields = (
            self.actions,
            self.descriptive_authorities,
            self.artifact_authorities,
            self.capability_contracts,
            self.source_paths,
            self.source_exclusions,
            self.collaboration_modules,
            self.collaboration_source_paths,
            self.package_dependencies,
            self.package_scripts,
        )
        if any(len(values) != len(set(values)) for values in tuple_fields):
            raise EndpointRoleContractError("endpoint_role_contract_duplicate_value")
        if len(self.collaboration_modules) != len(self.collaboration_source_paths):
            raise EndpointRoleContractError("endpoint_role_contract_collaboration_surface_invalid")
        if any(
            not isinstance(name, str) or not name or not isinstance(target, str) or not target
            for name, target in self.package_scripts
        ):
            raise EndpointRoleContractError("endpoint_role_contract_package_scripts_invalid")
        if len(tuple(name for name, _ in self.package_scripts)) != len(
            {name for name, _ in self.package_scripts}
        ):
            raise EndpointRoleContractError("endpoint_role_contract_package_scripts_invalid")
        if self.collaboration_writer_surface not in {"absent", "source-only-unwired"}:
            raise EndpointRoleContractError("endpoint_role_contract_writer_surface_invalid")
        if (self.collaboration_writer_surface == "absent") != (
            not self.collaboration_modules and not self.collaboration_source_paths
        ):
            raise EndpointRoleContractError("endpoint_role_contract_collaboration_surface_invalid")
        if self.role == PP_SERVER_BACKEND:
            if (
                self.collaboration_modules
                != tuple(module for module, _path in _COLLABORATION_FOUNDATION)
                or self.collaboration_source_paths
                != tuple(path for _module, path in _COLLABORATION_FOUNDATION)
                or self.collaboration_writer_surface != "source-only-unwired"
            ):
                raise EndpointRoleContractError(
                    "endpoint_role_contract_collaboration_surface_invalid"
                )
        elif self.collaboration_modules or self.collaboration_source_paths:
            raise EndpointRoleContractError("endpoint_role_contract_collaboration_surface_invalid")

    def includes_source_path(self, source_path: object) -> bool:
        """Return whether a repository-relative path may cross this package seam."""

        if not isinstance(source_path, str) or not source_path:
            return False

        def contains(root: str) -> bool:
            return source_path == root or source_path.startswith(f"{root}/")

        return any(contains(root) for root in self.source_paths) and not any(
            contains(root) for root in self.source_exclusions
        )

    @property
    def artifact_authority_label(self) -> str:
        """Return the deterministic OCI/Compose authority label."""

        return ",".join(self.artifact_authorities)

    def allows(self, action: object) -> bool:
        """Fail closed for malformed, unknown, or role-forbidden actions."""

        return isinstance(action, str) and action in self.actions


_ROLE_CONTRACTS: Mapping[str, EndpointRoleContract] = MappingProxyType(
    {
        PP_LOCAL_EDGE: EndpointRoleContract(
            role=PP_LOCAL_EDGE,
            actions=(
                "project-intent-submit",
                "bounded-state-projection-read",
            ),
            descriptive_authorities=(
                "loopback-status-projection",
                "local-session-cache",
                "hook-mcp-bridge",
            ),
            artifact_authorities=(
                "local-edge",
                "bounded-awareness-display",
                "bounded-event-submission",
            ),
            capability_contracts=(),
            package_kind="static",
            distribution_name="plastic-promise-local-edge",
            source_paths=(
                "deploy/local-edge/nginx.conf",
                "deploy/local-edge/entrypoint.sh",
                *_EDGE_STATIC_ASSET_PATHS,
            ),
        ),
        PP_SERVER_BACKEND: EndpointRoleContract(
            role=PP_SERVER_BACKEND,
            actions=(
                "canonical-sqlite-write",
                "inference-job-administer",
                "inference-result-accept",
                "task-queue-administer",
                "collaboration-agent-register",
                "collaboration-event-write",
                "collaboration-work-board-write",
                "collaboration-awareness-publish",
                "memory-proposal-promote",
                "knowledge-proposal-promote",
                "lancedb-promotion-decide",
                "merge-govern",
                "deployment-govern",
                "maintenance-govern",
                "deployment-receipt-persist",
            ),
            descriptive_authorities=(
                "canonical-sqlite-single-writer",
                "lancedb-promotion-decision",
                "deployment-receipt-persistence",
            ),
            artifact_authorities=(
                "agent-registry-authority",
                "work-board-authority",
                "canonical-memory-authority",
                "collaboration-event-writer",
            ),
            capability_contracts=(),
            package_kind="python",
            distribution_name="plastic-promise-server-backend",
            source_paths=(
                *_ROOT_RUNTIME_FILES,
                *_SERVER_RUNTIME_PACKAGES,
                *(path for _module, path in _COLLABORATION_FOUNDATION),
            ),
            source_exclusions=compute_package_manifest().server_source_exclusions,
            collaboration_modules=tuple(module for module, _path in _COLLABORATION_FOUNDATION),
            collaboration_source_paths=tuple(path for _module, path in _COLLABORATION_FOUNDATION),
            collaboration_writer_surface="source-only-unwired",
            package_dependencies=(
                "mcp>=1.0.0,<2.0.0",
                "lancedb>=0.34.0",
                "uvicorn[standard]>=0.27.0",
                "starlette>=0.36.0",
                "httpx>=0.27.0",
                "requests>=2.31.0",
                "PyYAML>=6.0.1,<7.0.0",
                "tomli>=2.0.1; python_version < '3.11'",
            ),
            package_scripts=(
                (
                    "plastic-promise-canonical-runtime",
                    "plastic_promise.deployment.runtime_lock:main",
                ),
                (
                    "plastic-promise-streamable-http",
                    "plastic_promise:main_streamable_http",
                ),
            ),
        ),
        PP_COMPUTE_NODE: EndpointRoleContract(
            role=PP_COMPUTE_NODE,
            actions=(
                "bounded-inference-lease",
                "derived-inference-return",
                "node-health-report",
                "node-resource-report",
                "model-identity-report",
                "timing-evidence-report",
            ),
            descriptive_authorities=("typed-derived-inference",),
            artifact_authorities=("compute-execution",),
            capability_contracts=compute_package_manifest().capability_contracts,
            package_kind="python",
            distribution_name="plastic-promise-compute-node",
            source_paths=(
                "plastic_promise/__init__.py",
                "plastic_promise/py.typed",
                "plastic_promise/local_inference_node",
            ),
            package_dependencies=(
                "uvicorn[standard]>=0.27.0",
                "starlette>=0.36.0",
                "httpx>=0.27.0",
                "requests>=2.31.0",
            ),
            package_scripts=(
                (
                    "plastic-promise-local-inference-node",
                    "plastic_promise.local_inference_node.server:main",
                ),
                (
                    "plastic-promise-local-inference-cache-plan",
                    "plastic_promise.local_inference_node.cache_planner:main",
                ),
            ),
        ),
    }
)


def endpoint_role_contract(role: object) -> EndpointRoleContract:
    """Return the sole repository-owned contract for ``role``."""

    if not isinstance(role, str):
        raise EndpointRoleContractError("endpoint_role_contract_role_invalid")
    policy = _ROLE_CONTRACTS.get(role)
    if policy is None:
        raise EndpointRoleContractError("endpoint_role_contract_role_invalid")
    return policy


@dataclass(frozen=True, slots=True)
class EdgeStaticSurfaceReceipt:
    """Deterministic evidence for the closed local-edge browser surface."""

    asset_paths: tuple[str, ...]
    operations: tuple[str, ...]
    surface_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EDGE_STATIC_SURFACE_SCHEMA_VERSION,
            "asset_paths": list(self.asset_paths),
            "operations": list(self.operations),
            "surface_digest": self.surface_digest,
        }


_EDGE_FORBIDDEN_TEXT = (
    "authorization",
    "bearer",
    "localstorage",
    "sessionstorage",
    "document.cookie",
    "xmlhttprequest",
    "websocket",
    "eventsource",
    "sendbeacon",
    "innerhtml",
    "outerhtml",
    "insertadjacenthtml",
    "eval(",
    "https://",
    "/api/dashboard/",
    "memory-proposals",
    "proposal-review",
    "/config/stage",
    "/config/revisions/",
    "/diagnostics/bundle",
)
_EDGE_FORBIDDEN_METHOD = re.compile(r"method\s*:\s*[\"'](?:PUT|PATCH|DELETE)[\"']", re.I)
_EDGE_INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)


def compile_edge_static_surface(assets: Mapping[str, str]) -> EdgeStaticSurfaceReceipt:
    """Validate and identify the exact inspect/preview-only edge bundle.

    The caller supplies decoded source text for every asset.  The compiler does
    not resolve paths or fetch dependencies, which keeps it usable from release
    preflight and deterministic tests.
    """

    if not isinstance(assets, Mapping) or tuple(sorted(assets)) != tuple(
        sorted(_EDGE_STATIC_ASSET_PATHS)
    ):
        raise EndpointRoleContractError("edge_static_surface_asset_matrix_invalid")
    for path, value in assets.items():
        if not isinstance(path, str) or not isinstance(value, str) or not value.strip():
            raise EndpointRoleContractError("edge_static_surface_asset_invalid")
        if len(value.encode("utf-8")) > 128 * 1024:
            raise EndpointRoleContractError("edge_static_surface_asset_too_large")
        normalized = value.casefold()
        if any(token in normalized for token in _EDGE_FORBIDDEN_TEXT):
            raise EndpointRoleContractError("edge_static_surface_privileged_surface_forbidden")
        if _EDGE_FORBIDDEN_METHOD.search(value):
            raise EndpointRoleContractError("edge_static_surface_mutation_method_forbidden")

    index = assets[_EDGE_STATIC_ASSET_PATHS[0]]
    stylesheet = assets[_EDGE_STATIC_ASSET_PATHS[1]]
    script = assets[_EDGE_STATIC_ASSET_PATHS[2]]
    if (
        '<link rel="stylesheet" href="/app.css">' not in index
        or '<script src="/app.js" defer></script>' not in index
        or "<script" in index.replace('<script src="/app.js" defer></script>', "")
        or _EDGE_INLINE_HANDLER.search(index)
        or "url(" in stylesheet.casefold()
        or "@import" in stylesheet.casefold()
    ):
        raise EndpointRoleContractError("edge_static_surface_document_invalid")
    required_script_contract = (
        'const CONFIG_PATH = "/pp-local-edge/v1/bridge-config.json";',
        'const OPERATION_PATHS = Object.freeze({ inspect: "/inspect", preview: "/preview" });',
        'credentials: "omit"',
        'referrerPolicy: "no-referrer"',
        'redirect: "error"',
    )
    if any(fragment not in script for fragment in required_script_contract):
        raise EndpointRoleContractError("edge_static_surface_operation_contract_invalid")
    if (
        script.count("fetch(") != 2
        or script.count("fetch(CONFIG_PATH,") != 1
        or script.count("fetch(requestUrl,") != 1
        or "const operationPath = OPERATION_PATHS[operation];" not in script
        or "requestUrl.pathname = bridge.pathname + operationPath;" not in script
    ):
        raise EndpointRoleContractError("edge_static_surface_transport_count_invalid")

    source_identity = {
        "schema_version": EDGE_STATIC_SURFACE_SCHEMA_VERSION,
        "operations": ["inspect", "preview"],
        "assets": [
            {
                "path": path,
                "sha256": hashlib.sha256(assets[path].encode("utf-8")).hexdigest(),
            }
            for path in _EDGE_STATIC_ASSET_PATHS
        ],
    }
    encoded = json.dumps(
        source_identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return EdgeStaticSurfaceReceipt(
        asset_paths=_EDGE_STATIC_ASSET_PATHS,
        operations=("inspect", "preview"),
        surface_digest=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
    )


__all__ = [
    "EDGE_STATIC_SURFACE_SCHEMA_VERSION",
    "ENDPOINT_ROLES",
    "PP_COMPUTE_NODE",
    "PP_LOCAL_EDGE",
    "PP_SERVER_BACKEND",
    "EdgeStaticSurfaceReceipt",
    "EndpointRoleContract",
    "EndpointRoleContractError",
    "compile_edge_static_surface",
    "endpoint_role_contract",
]
