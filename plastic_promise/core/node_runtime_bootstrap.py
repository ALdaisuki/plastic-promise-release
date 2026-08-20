"""Fail-closed server bootstrap for governed private inference-node indexing."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from plastic_promise.control_plane.store import ControlPlaneConfigStore
from plastic_promise.core.memory_index_node_runtime import (
    MemoryIndexNodeRuntime,
    MemoryIndexNodeRuntimeError,
    install_memory_index_node_runtime,
    open_server_memory_index_node_runtime,
)
from plastic_promise.core.node_governance import (
    NodeGovernanceError,
    NodeRegistration,
    open_server_node_governance,
    open_server_node_registration_authority,
)
from plastic_promise.core.node_private_runtime import RuntimePrivateNodeEndpointResolver
from plastic_promise.core.node_private_transport import (
    PrivateNodeEndpointResolver,
    PrivateNodeTransportProbe,
)
from plastic_promise.deployment import resolve_deployment_manifest

_MAX_MANIFEST_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


class _SafeConfigProvider(Protocol):
    def safe_config(self) -> object: ...

    def get_revision(self, revision_id: str) -> object: ...

    def compute_profile_digest(self, revision_id: str | None = None) -> str: ...


class _PinnedRevisionConfigProvider:
    """Read-only view of one staged revision for shadow materialization.

    The normal server process always uses the active Control snapshot.  A
    shadow LanceDB build is the one deliberate exception: it must exercise the
    candidate node identity before activation, while still using the same
    immutable control metadata and profile digest.  This adapter keeps that
    exception local to the build process and exposes no mutating methods.
    """

    def __init__(self, store: _SafeConfigProvider, revision: object) -> None:
        self._store = store
        self._revision = revision

    def safe_config(self) -> object:
        return self._revision

    def get_revision(self, revision_id: str) -> object:
        return self._store.get_revision(revision_id)

    def compute_profile_digest(self, revision_id: str | None = None) -> str:
        reader = getattr(self._store, "compute_profile_digest", None)
        if not callable(reader):
            raise NodeGovernanceError("node_controlled_revision_store_invalid")
        selected = revision_id or getattr(self._revision, "revision_id", None)
        return str(reader(selected))


@dataclass(frozen=True)
class NodeRuntimeBootstrapReport:
    """No-secret process-local bootstrap state for health and diagnostics."""

    state: str
    reason: str
    registered_nodes: int = 0
    config_revision: str | None = None


class BlockedMemoryIndexNodeRuntime:
    """Prevent an enabled governed route from silently using the old embedder."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def embedding_for_outbox(self, **_kwargs: object) -> object:
        raise MemoryIndexNodeRuntimeError(self._reason)


def bootstrap_memory_index_node_runtime(
    engine: Any,
    *,
    environ: Mapping[str, object] | None = None,
    control_config_factory: Callable[[Path, Mapping[str, object]], _SafeConfigProvider]
    | None = None,
    resolver_factory: Callable[[Mapping[str, object]], PrivateNodeEndpointResolver] | None = None,
    transport_factory: Callable[[PrivateNodeEndpointResolver], PrivateNodeTransportProbe]
    | None = None,
    runtime_factory: Callable[
        [_SafeConfigProvider, PrivateNodeTransportProbe], MemoryIndexNodeRuntime
    ]
    | None = None,
    installer: Callable[[Any, MemoryIndexNodeRuntime], None] | None = None,
) -> NodeRuntimeBootstrapReport:
    """Install private-node indexing only after all server-owned checks pass.

    Node routing is opt-in.  Once it is enabled, any missing deployment
    manifest, private resolver, schema, identity proof, or health check causes
    only derived indexing to defer.  Canonical SQLite writes keep working and
    the legacy ungoverned embedder is never selected by accident.
    """

    env = dict(os.environ if environ is None else environ)
    if str(env.get("PP_CONTROL_PLANE", "0")).strip() != "1":
        return _set_status(engine, "disabled", "node_routing_control_plane_disabled")
    open_control = control_config_factory or _open_control_config_readonly
    create_resolver = resolver_factory or RuntimePrivateNodeEndpointResolver.from_environment
    create_transport = transport_factory or PrivateNodeTransportProbe
    create_runtime = runtime_factory or open_server_memory_index_node_runtime
    install = installer or install_memory_index_node_runtime
    try:
        control = open_control(_control_root(env), env)
        # Shadow LanceDB builds receive the immutable staged revision through
        # an env-file.  Read that revision explicitly instead of silently
        # falling back to the currently active (usually text-only) snapshot.
        # The normal runtime path leaves this unset and remains active-only.
        staged_revision_id = str(env.get("PP_CONTROL_REVISION_ID") or "").strip()
        if staged_revision_id:
            if not re.fullmatch(r"cfg-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}", staged_revision_id):
                return _blocked(engine, "node_routing_revision_invalid", staged_revision_id)
            snapshot = control.get_revision(staged_revision_id)
            control = _PinnedRevisionConfigProvider(control, snapshot)
        else:
            snapshot = control.safe_config()
        config = getattr(snapshot, "config", None)
        revision_id = getattr(snapshot, "revision_id", None)
        routing = config.get("node_routing") if isinstance(config, Mapping) else None
        if not isinstance(routing, Mapping) or routing.get("enabled") is not True:
            return _set_status(engine, "disabled", "node_routing_disabled", revision_id)
        if not isinstance(revision_id, str) or not revision_id:
            return _blocked(engine, "node_routing_revision_unavailable", revision_id)
        allowed_node_ids = routing.get("allowed_node_ids")
        if not isinstance(allowed_node_ids, list) or not allowed_node_ids:
            return _blocked(engine, "node_routing_allowed_nodes_invalid", revision_id)
        deployment = _load_deployment(env)
        if set(allowed_node_ids) != set(deployment.node_ids):
            return _blocked(engine, "node_routing_deployment_nodes_mismatch", revision_id)
        deployed_nodes = {node.id: node for node in deployment.nodes}
        resolver = create_resolver(env)
        transport = create_transport(resolver)
        registrations: list[NodeRegistration] = []
        for node_id in allowed_node_ids:
            if not isinstance(node_id, str):
                return _blocked(engine, "node_routing_allowed_nodes_invalid", revision_id)
            registration = transport.discover_registration(
                node_id=node_id,
                max_concurrency=deployed_nodes[node_id].max_concurrency,
            )
            _require_route_identity(routing, registration)
            registrations.append(registration)
        registry = open_server_node_governance()
        authority = open_server_node_registration_authority()
        registered_nodes = 0
        for registration in registrations:
            first_observation = transport.probe(registration)
            authority.verify_deployment(deployment, registration, first_observation.health)
            verified = authority.verify_private_transport(
                control,
                config_revision=revision_id,
                registration=registration,
                transport=transport,
            )
            registry.register(verified)
            fresh_observation = transport.probe(verified.registration)
            registry.observe_health(fresh_observation.health)
            _record_profile_receipts(
                control,
                registry,
                revision_id=revision_id,
                routing=routing,
                registration=verified.registration,
                verified=verified,
            )
            registered_nodes += 1
        runtime = create_runtime(control, transport)
        install(engine, runtime)
        return _set_status(engine, "ready", "node_routing_ready", revision_id, registered_nodes)
    except NodeGovernanceError as exc:
        return _blocked(engine, exc.code, _revision_id_or_none(locals().get("revision_id")))
    except Exception as exc:
        code = getattr(exc, "code", "node_routing_bootstrap_unavailable")
        return _blocked(engine, str(code), _revision_id_or_none(locals().get("revision_id")))


def _blocked(engine: Any, reason: str, revision_id: str | None) -> NodeRuntimeBootstrapReport:
    installer = getattr(engine, "install_memory_index_node_runtime", None)
    if not callable(installer):
        raise NodeGovernanceError("node_index_engine_invalid")
    installer(BlockedMemoryIndexNodeRuntime(reason))
    return _set_status(engine, "blocked", reason, revision_id)


def _set_status(
    engine: Any,
    state: str,
    reason: str,
    revision_id: str | None = None,
    registered_nodes: int = 0,
) -> NodeRuntimeBootstrapReport:
    report = NodeRuntimeBootstrapReport(state, reason, registered_nodes, revision_id)
    updater = getattr(engine, "set_memory_index_node_runtime_status", None)
    if not callable(updater):
        raise NodeGovernanceError("node_index_engine_invalid")
    updater(
        {
            "state": report.state,
            "reason": report.reason,
            "registered_nodes": report.registered_nodes,
            "config_revision": report.config_revision,
        }
    )
    return report


def _open_control_config_readonly(
    root: Path,
    environ: Mapping[str, object],
) -> ControlPlaneConfigStore:
    return ControlPlaneConfigStore.open_existing_readonly(root, base_env=environ)


def _control_root(environ: Mapping[str, object]) -> Path:
    configured = environ.get("PP_CONTROL_ROOT")
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser()
    sqlite_path = Path(str(environ.get("PLASTIC_DB_PATH") or "data/db/plastic_memory.db"))
    state_root = (
        sqlite_path.parent.parent if sqlite_path.parent.name == "db" else sqlite_path.parent
    )
    return (state_root / "control").expanduser()


def _load_deployment(environ: Mapping[str, object]):
    configured = environ.get("PP_DEPLOYMENT_MANIFEST_PATH")
    if not isinstance(configured, str) or not configured.strip():
        raise NodeGovernanceError("node_deployment_manifest_missing")
    path = Path(configured).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise NodeGovernanceError("node_deployment_manifest_invalid")
    try:
        if not path.is_file() or path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise NodeGovernanceError("node_deployment_manifest_invalid")
        document = json.loads(path.read_text(encoding="utf-8"))
        return resolve_deployment_manifest(document)
    except NodeGovernanceError:
        raise
    except Exception as exc:
        raise NodeGovernanceError("node_deployment_manifest_invalid") from exc


def _require_route_identity(routing: Mapping[str, object], registration: object) -> None:
    identity = getattr(registration, "expected_identity", None)
    capabilities = getattr(registration, "capabilities", ())
    if identity is None or not isinstance(capabilities, tuple):
        raise NodeGovernanceError("node_private_registration_invalid")
    expected_embedding = routing.get("embedding_required_identity")
    expected_rerank = routing.get("rerank_required_identity")
    if not isinstance(expected_embedding, str) or identity.embedding_key != expected_embedding:
        raise NodeGovernanceError("node_private_embedding_identity_mismatch")
    if "rerank" in capabilities and (
        not isinstance(expected_rerank, str) or identity.rerank_key != expected_rerank
    ):
        raise NodeGovernanceError("node_private_rerank_identity_mismatch")


def _record_profile_receipts(
    control: _SafeConfigProvider,
    registry: object,
    *,
    revision_id: str,
    routing: Mapping[str, object],
    registration: NodeRegistration,
    verified: object,
) -> None:
    """Persist fresh profile-bound identity receipts after node health admission.

    The optional reader keeps legacy fixtures compatible; the production
    control-plane store exposes it and therefore binds every active route to
    the exact private compute projection that was validated.
    """

    digest_reader = getattr(control, "compute_profile_digest", None)
    if not callable(digest_reader):
        return
    try:
        profile_digest = digest_reader(revision_id)
    except Exception as exc:
        raise NodeGovernanceError("node_compute_profile_unavailable") from exc
    if not isinstance(profile_digest, str) or _SHA256_RE.fullmatch(profile_digest) is None:
        raise NodeGovernanceError("node_compute_profile_invalid")
    verification_receipt = getattr(verified, "receipt", None)
    recorder = getattr(registry, "record_identity_revalidation", None)
    if not callable(recorder):
        raise NodeGovernanceError("node_identity_receipt_recorder_unavailable")
    capabilities = set(registration.capabilities)
    required_by_capability = (
        ("embedding", routing.get("embedding_required_identity")),
        ("rerank", routing.get("rerank_required_identity")),
        ("structured-json", routing.get("structured_json_required_identity")),
    )
    for capability, required_identity in required_by_capability:
        if capability not in capabilities or not isinstance(required_identity, str):
            continue
        recorder(
            node_id=registration.node_id,
            config_revision=revision_id,
            required_identity=required_identity,
            profile_digest=profile_digest,
            verification_receipt=verification_receipt,
        )


def _revision_id_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
