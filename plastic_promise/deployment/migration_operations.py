"""Server-owned, typed migration orchestration.

This module is the execution seam for the split deployment topology.  The
Deployment Center and ``ppctl`` intentionally stop at inspection/planning;
only a server-owned :class:`MigrationOperations` instance may coordinate the
cut-over.  The implementation is deliberately adapter based: adapters receive
typed plans and fixed phase methods, never shell, Docker, SSH, SQLite, or
filesystem commands.

The source contract is safe to exercise with fakes. Production composition
uses the SQLite execution journal to persist grants, leases, fences, and safe
receipts in the pp-core canonical database; mutable phase adapters remain
separately injected and never receive arbitrary commands.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

from .collaboration_schema_migration import (
    COLLABORATION_SCHEMA_INSTALL_PHASE,
    COLLABORATION_SCHEMA_MANIFEST,
    COLLABORATION_SCHEMA_MANIFEST_SHA256,
    CanonicalBackupMigrationReceipt,
    CollaborationSchemaInstallReceipt,
    CollaborationSchemaMigrationError,
)
from .container_artifacts import ArtifactBundle
from .endpoint_contract import (
    PP_COMPUTE_NODE,
    PP_LOCAL_EDGE,
    PP_SERVER_BACKEND,
    ResolvedEndpointDeploymentPlan,
)
from .migration_journal import (
    DEFAULT_MIGRATION_LEASE_SECONDS,
    InMemoryMigrationExecutionJournal,
    MigrationExecutionIdentity,
    MigrationExecutionJournal,
    MigrationExecutionLease,
    MigrationJournalError,
)

MIGRATION_OPERATIONS_SCHEMA_VERSION = "plastic-promise-migration-operations/v2"
DEFAULT_PLAN_TTL_SECONDS = 300
DEFAULT_OBSERVATION_MAX_AGE_SECONDS = 120
MAX_SHORT_LIVED_TTL_SECONDS = 900

PHASE_STAGE_EDGE_COMPUTE = "stage-verify-edge-compute"
PHASE_CANONICAL_REHEARSAL = "canonical-rehearsal"
PHASE_STOP_LEGACY = "stop-legacy"
PHASE_CANONICAL_BACKUP_MIGRATION = "canonical-backup-migration"
PHASE_COLLABORATION_SCHEMA_INSTALL = COLLABORATION_SCHEMA_INSTALL_PHASE
PHASE_START_BACKEND = "start-backend"
PHASE_SHADOW_REBUILD_PROMOTE = "shadow-rebuild-verify-promote"
PHASE_ENABLE_MAINTENANCE = "enable-maintenance"
PHASE_RETENTION_CACHE_POLICY = "retention-cache-policy"

APPLY_PHASES = (
    PHASE_STAGE_EDGE_COMPUTE,
    PHASE_CANONICAL_REHEARSAL,
    PHASE_STOP_LEGACY,
    PHASE_CANONICAL_BACKUP_MIGRATION,
    PHASE_COLLABORATION_SCHEMA_INSTALL,
    PHASE_START_BACKEND,
    PHASE_SHADOW_REBUILD_PROMOTE,
    PHASE_ENABLE_MAINTENANCE,
    PHASE_RETENTION_CACHE_POLICY,
)

ROLLBACK_PHASES = (
    "disable-maintenance",
    "revert-derived-selection",
    "stop-new-backend",
    "canonical-restore",
    "restart-legacy",
)

OPERATION_PHASE_MANIFEST = APPLY_PHASES + ROLLBACK_PHASES

_SAFE_REF = re.compile(r"^[a-z][a-z0-9:_-]{1,127}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_:-]{1,127}$")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}$")


class MigrationOperationsError(ValueError):
    """A stable, non-secret error from the migration-operations boundary."""

    def __init__(self, code: str, *, category: str = "invalid") -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("migration_error_code_invalid")
        if category not in {"invalid", "conflict", "stale", "unavailable"}:
            raise ValueError("migration_error_category_invalid")
        self.code = code
        self.category = category
        super().__init__(code)

    def public_json(self) -> dict[str, str]:
        return {"code": self.code, "category": self.category}


def _require_ref(value: object, code: str) -> str:
    if not isinstance(value, str) or _SAFE_REF.fullmatch(value) is None:
        raise MigrationOperationsError(code)
    return value


def _require_code(value: object, code: str) -> str:
    if not isinstance(value, str) or _SAFE_CODE.fullmatch(value) is None:
        raise MigrationOperationsError(code)
    return value


def _require_digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise MigrationOperationsError(code)
    return value


def _require_positive_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MigrationOperationsError(code)
    return value


def _require_nonnegative_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MigrationOperationsError(code)
    return value


def _require_utc(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MigrationOperationsError(code)
    return value.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(payload: Mapping[str, object]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


def _safe_digest(value: object, code: str) -> str:
    return _require_digest(value, code)


def _stable_observation_payload(observations: MigrationObservations) -> dict[str, object]:
    """Render state that should trigger drift, excluding wall-clock timestamps."""

    return {
        "canonical": observations.canonical.stable_dict(),
        "runtime": observations.runtime.stable_dict(),
        "nodes": observations.nodes.stable_dict(),
        "derived": observations.derived.stable_dict(),
    }


def _observation_digest(observations: MigrationObservations) -> str:
    return _digest(_stable_observation_payload(observations))


def _artifact_bundle_digest(bundle: ArtifactBundle) -> str:
    """Hash only the bundle's immutable, secret-free inspection projection."""

    return _digest(bundle.inspection_projection())


def _manifest_digest(manifest: Sequence[str]) -> str:
    """Hash one exact ordered manifest using the migration canonical JSON."""

    payload = json.dumps(
        list(manifest),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _validate_artifact_topology_compatibility(
    topology: ResolvedEndpointDeploymentPlan,
    bundle: ArtifactBundle,
) -> None:
    """Require the immutable artifact matrix to fit the resolved V2 roles.

    The artifact compiler already validates each descriptor internally.  This
    second, cross-contract gate prevents a valid bundle for one profile from
    being accidentally paired with a different resolved topology.
    """

    if bundle.plan.request.profile_id != topology.profile_id:
        raise MigrationOperationsError("migration_artifact_profile_mismatch")

    declared_roles = {endpoint.role for endpoint in topology.endpoints}
    materialized_roles = {item.role for item in bundle.materializations}
    if not materialized_roles.issubset(declared_roles):
        raise MigrationOperationsError("migration_artifact_role_not_declared")

    for required_role in (PP_LOCAL_EDGE, PP_SERVER_BACKEND):
        if required_role not in materialized_roles:
            raise MigrationOperationsError("migration_artifact_required_role_missing")

    declared_compute = PP_COMPUTE_NODE in declared_roles
    materialized_compute = PP_COMPUTE_NODE in materialized_roles
    if declared_compute != materialized_compute:
        raise MigrationOperationsError("migration_artifact_compute_role_mismatch")

    if declared_compute:
        declared_capabilities = {
            f"{capability.kind}/{capability.contract_version.split('/', 1)[1]}"
            for endpoint in topology.endpoints
            if endpoint.role == PP_COMPUTE_NODE
            for capability in endpoint.capabilities
        }
        artifact_capabilities = {
            capability
            for artifact in bundle.plan.artifacts
            if artifact.role == PP_COMPUTE_NODE
            for capability in artifact.capabilities
        }
        if not declared_capabilities.issubset(artifact_capabilities):
            raise MigrationOperationsError("migration_artifact_compute_capability_mismatch")


@dataclass(frozen=True)
class TopologyDigest:
    """Safe topology key used when a plan deliberately retains only a digest."""

    manifest_digest: str

    def __post_init__(self) -> None:
        _safe_digest(self.manifest_digest, "migration_topology_digest_invalid")

    def to_dict(self) -> dict[str, str]:
        return {"manifest_digest": self.manifest_digest}


@dataclass(frozen=True)
class CanonicalStateObservation:
    """Safe canonical-state evidence; ``pp-server-backend`` is the sole writer."""

    generation: int
    state_digest: str
    backup_ready: bool = True
    writer_endpoint_id: str = PP_SERVER_BACKEND
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.generation, "migration_canonical_generation_invalid")
        _safe_digest(self.state_digest, "migration_canonical_digest_invalid")
        if not isinstance(self.backup_ready, bool):
            raise MigrationOperationsError("migration_canonical_backup_ready_invalid")
        if self.writer_endpoint_id != PP_SERVER_BACKEND:
            raise MigrationOperationsError("migration_canonical_writer_invalid")
        _require_utc(self.observed_at, "migration_observation_timestamp_invalid")

    def stable_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "state_digest": self.state_digest,
            "backup_ready": self.backup_ready,
            "writer_endpoint_id": self.writer_endpoint_id,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.stable_dict(), "observed_at": _utc(self.observed_at)}


@dataclass(frozen=True)
class RuntimeObservation:
    """Safe runtime state used to gate a controlled cut-over."""

    generation: int
    legacy_active: bool
    backend_active: bool
    runtime_digest: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.generation, "migration_runtime_generation_invalid")
        if not isinstance(self.legacy_active, bool) or not isinstance(self.backend_active, bool):
            raise MigrationOperationsError("migration_runtime_state_invalid")
        _safe_digest(self.runtime_digest, "migration_runtime_digest_invalid")
        _require_utc(self.observed_at, "migration_observation_timestamp_invalid")

    def stable_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "legacy_active": self.legacy_active,
            "backend_active": self.backend_active,
            "runtime_digest": self.runtime_digest,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.stable_dict(), "observed_at": _utc(self.observed_at)}


@dataclass(frozen=True)
class NodeReadinessObservation:
    """Safe readiness projection for edge/compute nodes."""

    generation: int
    ready: bool
    topology_digest: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.generation, "migration_node_generation_invalid")
        if not isinstance(self.ready, bool):
            raise MigrationOperationsError("migration_node_readiness_invalid")
        _safe_digest(self.topology_digest, "migration_node_topology_digest_invalid")
        _require_utc(self.observed_at, "migration_observation_timestamp_invalid")

    def stable_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "ready": self.ready,
            "topology_digest": self.topology_digest,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.stable_dict(), "observed_at": _utc(self.observed_at)}


@dataclass(frozen=True)
class DerivedGenerationObservation:
    """Safe LanceDB/derived-index generation projection."""

    active_generation: int
    generation_digest: str
    promotion_ready: bool = True
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.active_generation, "migration_derived_generation_invalid")
        _safe_digest(self.generation_digest, "migration_derived_digest_invalid")
        if not isinstance(self.promotion_ready, bool):
            raise MigrationOperationsError("migration_derived_promotion_ready_invalid")
        _require_utc(self.observed_at, "migration_observation_timestamp_invalid")

    def stable_dict(self) -> dict[str, object]:
        return {
            "active_generation": self.active_generation,
            "generation_digest": self.generation_digest,
            "promotion_ready": self.promotion_ready,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.stable_dict(), "observed_at": _utc(self.observed_at)}


@dataclass(frozen=True)
class MigrationObservations:
    """The four fresh, safe observations bound into a migration plan."""

    canonical: CanonicalStateObservation
    runtime: RuntimeObservation
    nodes: NodeReadinessObservation
    derived: DerivedGenerationObservation

    def __post_init__(self) -> None:
        if not all(
            isinstance(
                item,
                (
                    CanonicalStateObservation,
                    RuntimeObservation,
                    NodeReadinessObservation,
                    DerivedGenerationObservation,
                ),
            )
            for item in (self.canonical, self.runtime, self.nodes, self.derived)
        ):
            raise MigrationOperationsError("migration_observations_typed_required")

    @property
    def observed_at(self) -> datetime:
        return max(
            item.observed_at for item in (self.canonical, self.runtime, self.nodes, self.derived)
        )

    @property
    def digest(self) -> str:
        return _observation_digest(self)

    def is_fresh(self, now: datetime, max_age_seconds: int) -> bool:
        now = _require_utc(now, "migration_observation_timestamp_invalid")
        max_age_seconds = _require_positive_int(
            max_age_seconds, "migration_observation_max_age_invalid"
        )
        observations = (self.canonical, self.runtime, self.nodes, self.derived)
        return all(
            0 <= (now - item.observed_at).total_seconds() <= max_age_seconds
            for item in observations
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "canonical": self.canonical.to_dict(),
            "runtime": self.runtime.to_dict(),
            "nodes": self.nodes.to_dict(),
            "derived": self.derived.to_dict(),
            "digest": self.digest,
        }


@dataclass(frozen=True)
class MigrationIntent:
    """Typed, secret-free input for one server-owned migration operation."""

    topology: ResolvedEndpointDeploymentPlan
    artifact_bundle: ArtifactBundle
    installation_ref: str = "installation"
    operation_ref: str = "migration"

    def __post_init__(self) -> None:
        if not isinstance(self.topology, ResolvedEndpointDeploymentPlan):
            raise MigrationOperationsError("migration_topology_resolved_required")
        if not isinstance(self.artifact_bundle, ArtifactBundle):
            raise MigrationOperationsError("migration_artifact_bundle_required")
        _validate_artifact_topology_compatibility(self.topology, self.artifact_bundle)
        _require_ref(self.installation_ref, "migration_installation_reference_invalid")
        _require_ref(self.operation_ref, "migration_operation_reference_invalid")

    @property
    def resolved_topology(self) -> ResolvedEndpointDeploymentPlan:
        return self.topology

    @property
    def artifacts(self) -> ArtifactBundle:
        return self.artifact_bundle


@dataclass(frozen=True)
class MigrationOperationPlan:
    """Short-lived plan whose hash is inspection evidence, never a grant."""

    operation_ref: str
    installation_ref: str
    topology_digest: str
    artifact_bundle_digest: str
    observations: MigrationObservations
    created_at: datetime
    expires_at: datetime
    phase_manifest: tuple[str, ...]
    phase_manifest_sha256: str
    schema_manifest: tuple[str, ...]
    schema_manifest_sha256: str
    plan_hash: str
    schema_version: str = MIGRATION_OPERATIONS_SCHEMA_VERSION
    # The typed bindings are retained only in server memory for adapter calls;
    # ``to_dict`` exposes digests, never raw manifests or artifact internals.
    resolved_topology: ResolvedEndpointDeploymentPlan | None = field(
        default=None, repr=False, compare=False
    )
    artifact_bundle: ArtifactBundle | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema_version != MIGRATION_OPERATIONS_SCHEMA_VERSION:
            raise MigrationOperationsError("migration_plan_schema_unsupported")
        _require_ref(self.operation_ref, "migration_operation_reference_invalid")
        _require_ref(self.installation_ref, "migration_installation_reference_invalid")
        _safe_digest(self.topology_digest, "migration_topology_digest_invalid")
        _safe_digest(self.artifact_bundle_digest, "migration_artifact_bundle_digest_invalid")
        if not isinstance(self.observations, MigrationObservations):
            raise MigrationOperationsError("migration_observations_typed_required")
        if self.phase_manifest != OPERATION_PHASE_MANIFEST:
            raise MigrationOperationsError("migration_phase_manifest_mismatch")
        _safe_digest(
            self.phase_manifest_sha256,
            "migration_phase_manifest_digest_invalid",
        )
        if self.phase_manifest_sha256 != _manifest_digest(self.phase_manifest):
            raise MigrationOperationsError("migration_phase_manifest_digest_mismatch")
        if self.schema_manifest != COLLABORATION_SCHEMA_MANIFEST:
            raise MigrationOperationsError("migration_schema_manifest_mismatch")
        _safe_digest(
            self.schema_manifest_sha256,
            "migration_schema_manifest_digest_invalid",
        )
        if self.schema_manifest_sha256 != COLLABORATION_SCHEMA_MANIFEST_SHA256:
            raise MigrationOperationsError("migration_schema_manifest_digest_mismatch")
        if self.resolved_topology is not None:
            if not isinstance(self.resolved_topology, ResolvedEndpointDeploymentPlan):
                raise MigrationOperationsError("migration_topology_resolved_required")
            if self.resolved_topology.manifest_digest != self.topology_digest:
                raise MigrationOperationsError("migration_topology_digest_mismatch")
        if self.artifact_bundle is not None:
            if not isinstance(self.artifact_bundle, ArtifactBundle):
                raise MigrationOperationsError("migration_artifact_bundle_required")
            if _artifact_bundle_digest(self.artifact_bundle) != self.artifact_bundle_digest:
                raise MigrationOperationsError("migration_artifact_bundle_digest_mismatch")
        if self.resolved_topology is not None and self.artifact_bundle is not None:
            _validate_artifact_topology_compatibility(self.resolved_topology, self.artifact_bundle)
        created = _require_utc(self.created_at, "migration_plan_timestamp_invalid")
        expires = _require_utc(self.expires_at, "migration_plan_timestamp_invalid")
        if expires <= created:
            raise MigrationOperationsError("migration_plan_expiry_invalid")
        if expires - created > timedelta(seconds=MAX_SHORT_LIVED_TTL_SECONDS):
            raise MigrationOperationsError("migration_plan_ttl_excessive")
        _safe_digest(self.plan_hash, "migration_plan_hash_invalid")
        expected = _plan_hash(
            operation_ref=self.operation_ref,
            installation_ref=self.installation_ref,
            topology_digest=self.topology_digest,
            artifact_bundle_digest=self.artifact_bundle_digest,
            observations=self.observations,
            created_at=created,
            expires_at=expires,
            phase_manifest=self.phase_manifest,
            phase_manifest_sha256=self.phase_manifest_sha256,
            schema_manifest=self.schema_manifest,
            schema_manifest_sha256=self.schema_manifest_sha256,
        )
        if expected != self.plan_hash:
            raise MigrationOperationsError("migration_plan_hash_mismatch")

    @property
    def phases(self) -> tuple[str, ...]:
        return APPLY_PHASES

    @property
    def observation_digest(self) -> str:
        return self.observations.digest

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation_ref": self.operation_ref,
            "installation_ref": self.installation_ref,
            "topology_digest": self.topology_digest,
            "artifact_bundle_digest": self.artifact_bundle_digest,
            "observations": self.observations.to_dict(),
            "created_at": _utc(self.created_at),
            "expires_at": _utc(self.expires_at),
            "phase_manifest": list(self.phase_manifest),
            "phase_manifest_sha256": self.phase_manifest_sha256,
            "schema_manifest": list(self.schema_manifest),
            "schema_manifest_sha256": self.schema_manifest_sha256,
            "plan_hash": self.plan_hash,
            "phases": list(self.phases),
            "execution_authority": PP_SERVER_BACKEND,
        }


@dataclass(frozen=True)
class MigrationExecutionGrant:
    """A short-lived, secret-free authorization bound to one plan hash."""

    plan_hash: str
    grant_id: str
    issued_at: datetime
    expires_at: datetime
    authority: str = PP_SERVER_BACKEND

    def __post_init__(self) -> None:
        _safe_digest(self.plan_hash, "migration_grant_plan_hash_invalid")
        _require_ref(self.grant_id, "migration_grant_id_invalid")
        issued = _require_utc(self.issued_at, "migration_grant_timestamp_invalid")
        expires = _require_utc(self.expires_at, "migration_grant_timestamp_invalid")
        if expires <= issued:
            raise MigrationOperationsError("migration_grant_expiry_invalid")
        if expires - issued > timedelta(seconds=MAX_SHORT_LIVED_TTL_SECONDS):
            raise MigrationOperationsError("migration_grant_ttl_excessive")
        if self.authority != PP_SERVER_BACKEND:
            raise MigrationOperationsError("migration_grant_authority_invalid")

    def is_valid_for(self, plan: MigrationOperationPlan, now: datetime) -> bool:
        now = _require_utc(now, "migration_grant_timestamp_invalid")
        return (
            self.plan_hash == plan.plan_hash
            and self.issued_at >= plan.created_at
            and self.expires_at <= plan.expires_at
            and self.issued_at <= now < self.expires_at
            and now < plan.expires_at
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "plan_hash": self.plan_hash,
            "issued_at": _utc(self.issued_at),
            "expires_at": _utc(self.expires_at),
            "authority": self.authority,
        }


# Short alias used by composition roots that call this simply an execution
# grant.  Both names remain typed and intentionally secret-free.
ExecutionGrant = MigrationExecutionGrant
MigrationPlan = MigrationOperationPlan
MigrationGrant = MigrationExecutionGrant


@dataclass(frozen=True)
class MigrationPreflight:
    """Read-only admission result."""

    ok: bool
    reason_codes: tuple[str, ...]
    plan_hash: str
    observed_digest: str | None
    checked_at: datetime

    @property
    def failure_codes(self) -> tuple[str, ...]:
        return self.reason_codes

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reason_codes": list(self.reason_codes),
            "plan_hash": self.plan_hash,
            "observed_digest": self.observed_digest,
            "checked_at": _utc(self.checked_at),
        }


@dataclass(frozen=True)
class MigrationPhaseRecord:
    """Secret-free phase result persisted by the configured execution journal."""

    phase: str
    outcome: str
    reason_code: str = "migration_phase_completed"

    def __post_init__(self) -> None:
        if self.phase not in APPLY_PHASES + ROLLBACK_PHASES:
            raise MigrationOperationsError("migration_phase_invalid")
        if self.outcome not in {"completed", "failed", "skipped"}:
            raise MigrationOperationsError("migration_phase_outcome_invalid")
        _require_code(self.reason_code, "migration_phase_reason_invalid")

    def to_dict(self) -> dict[str, str]:
        return {"phase": self.phase, "outcome": self.outcome, "reason_code": self.reason_code}


@dataclass(frozen=True)
class MigrationApplyResult:
    """Safe in-memory result for dry-run, success, rejection, or rollback."""

    accepted: bool
    dry_run: bool
    outcome: str
    reason_code: str
    plan_hash: str
    phases: tuple[MigrationPhaseRecord, ...] = ()
    rollback_attempted: bool = False
    rollback_completed: bool = False
    rollback_reason_codes: tuple[str, ...] = ()
    canonical_backup_receipt_sha256: str = ""
    collaboration_schema_receipt_sha256: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in {
            "dry-run",
            "applied",
            "rejected",
            "rolled-back",
            "recovery-required",
        }:
            raise MigrationOperationsError("migration_result_outcome_invalid")
        _require_code(self.reason_code, "migration_result_reason_invalid")
        _safe_digest(self.plan_hash, "migration_plan_hash_invalid")
        if not isinstance(self.dry_run, bool) or not isinstance(self.accepted, bool):
            raise MigrationOperationsError("migration_result_flag_invalid")
        for value in (
            self.canonical_backup_receipt_sha256,
            self.collaboration_schema_receipt_sha256,
        ):
            if value:
                _safe_digest(value, "migration_result_receipt_digest_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "dry_run": self.dry_run,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "plan_hash": self.plan_hash,
            "phases": [phase.to_dict() for phase in self.phases],
            "rollback_attempted": self.rollback_attempted,
            "rollback_completed": self.rollback_completed,
            "rollback_reason_codes": list(self.rollback_reason_codes),
            "canonical_backup_receipt_sha256": self.canonical_backup_receipt_sha256,
            "collaboration_schema_receipt_sha256": self.collaboration_schema_receipt_sha256,
        }


@dataclass(frozen=True)
class MigrationExecutionContext:
    """Typed phase input carrying the durable server fencing generation."""

    plan: MigrationOperationPlan
    lease: MigrationExecutionLease

    def __post_init__(self) -> None:
        if not isinstance(self.plan, MigrationOperationPlan):
            raise MigrationOperationsError("migration_plan_typed_required")
        if not isinstance(self.lease, MigrationExecutionLease):
            raise MigrationOperationsError("migration_execution_lease_required")
        if self.lease.plan_hash != self.plan.plan_hash:
            raise MigrationOperationsError("migration_execution_lease_plan_mismatch")

    @property
    def fencing_generation(self) -> int:
        return self.lease.fencing_generation

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_hash": self.plan.plan_hash,
            "installation_ref": self.plan.installation_ref,
            "operation_ref": self.plan.operation_ref,
            "operation_id": self.lease.operation_id,
            "lease_owner_ref": self.lease.owner_ref,
            "fencing_generation": self.lease.fencing_generation,
            "lease_expires_at": _utc(self.lease.expires_at),
        }


@runtime_checkable
class MigrationObservationAdapter(Protocol):
    """Read-only observation seam for canonical/runtime/node/derived state."""

    def observe(
        self,
        topology: ResolvedEndpointDeploymentPlan | TopologyDigest,
        installation_ref: str,
    ) -> MigrationObservations:
        """Return a fresh, safe observation snapshot."""


@runtime_checkable
class EdgeComputeMigrationAdapter(Protocol):
    def stage_and_verify(self, context: MigrationExecutionContext) -> None: ...


@runtime_checkable
class CanonicalStateMigrationAdapter(Protocol):
    def rehearse(self, context: MigrationExecutionContext) -> None: ...

    def backup_and_migrate(
        self,
        context: MigrationExecutionContext,
    ) -> CanonicalBackupMigrationReceipt: ...

    def restore(self, context: MigrationExecutionContext) -> None: ...


@runtime_checkable
class CollaborationSchemaMigrationAdapter(Protocol):
    """Deployment-owned phase-4 seam; runtime construction remains verify-only."""

    def install(
        self,
        context: MigrationExecutionContext,
        backup_receipt: CanonicalBackupMigrationReceipt,
    ) -> CollaborationSchemaInstallReceipt: ...


@runtime_checkable
class RuntimeMigrationAdapter(Protocol):
    def stop_legacy(self, context: MigrationExecutionContext) -> None: ...

    def start_backend(self, context: MigrationExecutionContext) -> None: ...

    def stop_backend(self, context: MigrationExecutionContext) -> None: ...

    def restart_legacy(self, context: MigrationExecutionContext) -> None: ...


@runtime_checkable
class DerivedIndexMigrationAdapter(Protocol):
    def shadow_rebuild_verify_promote(self, context: MigrationExecutionContext) -> None: ...

    def revert_selection(self, context: MigrationExecutionContext) -> None: ...


@runtime_checkable
class MaintenanceMigrationAdapter(Protocol):
    def enable(self, context: MigrationExecutionContext) -> None: ...

    def disable(self, context: MigrationExecutionContext) -> None: ...


@runtime_checkable
class RetentionCacheMigrationAdapter(Protocol):
    def apply(self, context: MigrationExecutionContext) -> None: ...


@dataclass(frozen=True)
class MigrationAdapters:
    """Fixed typed mutable seams used by :class:`MigrationOperations`."""

    edge_compute: EdgeComputeMigrationAdapter
    canonical_state: CanonicalStateMigrationAdapter
    collaboration_schema: CollaborationSchemaMigrationAdapter
    runtime: RuntimeMigrationAdapter
    derived_index: DerivedIndexMigrationAdapter
    maintenance: MaintenanceMigrationAdapter
    retention_cache: RetentionCacheMigrationAdapter

    def __post_init__(self) -> None:
        requirements: tuple[tuple[object, type[Protocol], str], ...] = (
            (
                self.edge_compute,
                EdgeComputeMigrationAdapter,
                "migration_edge_compute_adapter_required",
            ),
            (
                self.canonical_state,
                CanonicalStateMigrationAdapter,
                "migration_canonical_state_adapter_required",
            ),
            (
                self.collaboration_schema,
                CollaborationSchemaMigrationAdapter,
                "migration_collaboration_schema_adapter_required",
            ),
            (self.runtime, RuntimeMigrationAdapter, "migration_runtime_adapter_required"),
            (
                self.derived_index,
                DerivedIndexMigrationAdapter,
                "migration_derived_index_adapter_required",
            ),
            (
                self.maintenance,
                MaintenanceMigrationAdapter,
                "migration_maintenance_adapter_required",
            ),
            (
                self.retention_cache,
                RetentionCacheMigrationAdapter,
                "migration_retention_cache_adapter_required",
            ),
        )
        for adapter, adapter_protocol, error_code in requirements:
            if not isinstance(adapter, adapter_protocol):
                raise MigrationOperationsError(error_code)


def _plan_hash(
    *,
    operation_ref: str,
    installation_ref: str,
    topology_digest: str,
    artifact_bundle_digest: str,
    observations: MigrationObservations,
    created_at: datetime,
    expires_at: datetime,
    phase_manifest: tuple[str, ...],
    phase_manifest_sha256: str,
    schema_manifest: tuple[str, ...],
    schema_manifest_sha256: str,
) -> str:
    return _digest(
        {
            "schema_version": MIGRATION_OPERATIONS_SCHEMA_VERSION,
            "operation_ref": operation_ref,
            "installation_ref": installation_ref,
            "topology_digest": topology_digest,
            "artifact_bundle_digest": artifact_bundle_digest,
            "observation_digest": observations.digest,
            "created_at": _utc(created_at),
            "expires_at": _utc(expires_at),
            "phase_manifest": list(phase_manifest),
            "phase_manifest_sha256": phase_manifest_sha256,
            "schema_manifest": list(schema_manifest),
            "schema_manifest_sha256": schema_manifest_sha256,
        }
    )


class MigrationOperations:
    """Plan, preflight, and execute one server-owned migration operation."""

    def __init__(
        self,
        observation_adapter: MigrationObservationAdapter | None = None,
        adapters: MigrationAdapters | None = None,
        *,
        observer: MigrationObservationAdapter | None = None,
        mutable_adapters: MigrationAdapters | None = None,
        execution_journal: MigrationExecutionJournal | None = None,
        clock: Callable[[], datetime] | None = None,
        plan_ttl_seconds: int = DEFAULT_PLAN_TTL_SECONDS,
        observation_max_age_seconds: int = DEFAULT_OBSERVATION_MAX_AGE_SECONDS,
        migration_lease_seconds: int = DEFAULT_MIGRATION_LEASE_SECONDS,
    ) -> None:
        if observation_adapter is None:
            observation_adapter = observer
        elif observer is not None and observer is not observation_adapter:
            raise MigrationOperationsError("migration_observation_adapter_ambiguous")
        if adapters is None:
            adapters = mutable_adapters
        elif mutable_adapters is not None and mutable_adapters is not adapters:
            raise MigrationOperationsError("migration_adapters_ambiguous")
        if not isinstance(observation_adapter, MigrationObservationAdapter):
            raise MigrationOperationsError("migration_observation_adapter_required")
        if not isinstance(adapters, MigrationAdapters):
            raise MigrationOperationsError("migration_adapters_required")
        if execution_journal is None:
            execution_journal = InMemoryMigrationExecutionJournal()
        if not isinstance(execution_journal, MigrationExecutionJournal):
            raise MigrationOperationsError("migration_execution_journal_required")
        self._observation_adapter = observation_adapter
        self._adapters = adapters
        self._execution_journal = execution_journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._plan_ttl_seconds = _require_positive_int(
            plan_ttl_seconds, "migration_plan_ttl_invalid"
        )
        if self._plan_ttl_seconds > MAX_SHORT_LIVED_TTL_SECONDS:
            raise MigrationOperationsError("migration_plan_ttl_excessive")
        self._observation_max_age_seconds = _require_positive_int(
            observation_max_age_seconds, "migration_observation_max_age_invalid"
        )
        if self._observation_max_age_seconds > MAX_SHORT_LIVED_TTL_SECONDS:
            raise MigrationOperationsError("migration_observation_max_age_excessive")
        self._migration_lease_seconds = _require_positive_int(
            migration_lease_seconds, "migration_lease_ttl_invalid"
        )
        if self._migration_lease_seconds > MAX_SHORT_LIVED_TTL_SECONDS:
            raise MigrationOperationsError("migration_lease_ttl_excessive")

    def _now(self) -> datetime:
        return _require_utc(self._clock(), "migration_clock_invalid")

    def _observe(self, intent: MigrationIntent) -> MigrationObservations:
        try:
            observations = self._observation_adapter.observe(
                intent.topology, intent.installation_ref
            )
        except Exception as exc:  # noqa: BLE001 - adapter details stay private
            code = _reason_from_exception(exc, "migration_observation_unavailable")
            raise MigrationOperationsError(code, category="unavailable") from None
        if not isinstance(observations, MigrationObservations):
            raise MigrationOperationsError("migration_observation_typed_required")
        if not observations.is_fresh(self._now(), self._observation_max_age_seconds):
            raise MigrationOperationsError("migration_observation_stale", category="stale")
        return observations

    def plan(self, intent: MigrationIntent) -> MigrationOperationPlan:
        """Resolve fresh evidence and create a short-lived deterministic plan."""

        if not isinstance(intent, MigrationIntent):
            raise MigrationOperationsError("migration_intent_typed_required")
        now = self._now()
        observations = self._observe(intent)
        expires = now + timedelta(seconds=self._plan_ttl_seconds)
        topology_digest = intent.topology.manifest_digest
        artifact_digest = _artifact_bundle_digest(intent.artifact_bundle)
        phase_manifest = OPERATION_PHASE_MANIFEST
        phase_manifest_sha256 = _manifest_digest(phase_manifest)
        schema_manifest = COLLABORATION_SCHEMA_MANIFEST
        schema_manifest_sha256 = COLLABORATION_SCHEMA_MANIFEST_SHA256
        plan_hash = _plan_hash(
            operation_ref=intent.operation_ref,
            installation_ref=intent.installation_ref,
            topology_digest=topology_digest,
            artifact_bundle_digest=artifact_digest,
            observations=observations,
            created_at=now,
            expires_at=expires,
            phase_manifest=phase_manifest,
            phase_manifest_sha256=phase_manifest_sha256,
            schema_manifest=schema_manifest,
            schema_manifest_sha256=schema_manifest_sha256,
        )
        return MigrationOperationPlan(
            operation_ref=intent.operation_ref,
            installation_ref=intent.installation_ref,
            topology_digest=topology_digest,
            artifact_bundle_digest=artifact_digest,
            observations=observations,
            created_at=now,
            expires_at=expires,
            phase_manifest=phase_manifest,
            phase_manifest_sha256=phase_manifest_sha256,
            schema_manifest=schema_manifest,
            schema_manifest_sha256=schema_manifest_sha256,
            plan_hash=plan_hash,
            resolved_topology=intent.topology,
            artifact_bundle=intent.artifact_bundle,
        )

    @staticmethod
    def _execution_identity(
        plan: MigrationOperationPlan, grant: MigrationExecutionGrant
    ) -> MigrationExecutionIdentity:
        return MigrationExecutionIdentity(
            installation_ref=plan.installation_ref,
            operation_ref=plan.operation_ref,
            plan_hash=plan.plan_hash,
            phase_manifest=plan.phase_manifest,
            phase_manifest_sha256=plan.phase_manifest_sha256,
            schema_manifest=plan.schema_manifest,
            schema_manifest_sha256=plan.schema_manifest_sha256,
            grant_id=grant.grant_id,
            grant_issued_at=grant.issued_at,
            grant_expires_at=grant.expires_at,
        )

    def register_grant(
        self, plan: MigrationOperationPlan, grant: MigrationExecutionGrant
    ) -> MigrationExecutionGrant:
        """Persist one server-issued grant before any mutable apply call."""

        if not isinstance(plan, MigrationOperationPlan):
            raise MigrationOperationsError("migration_plan_typed_required")
        if not isinstance(grant, MigrationExecutionGrant):
            raise MigrationOperationsError("migration_grant_typed_required")
        if not grant.is_valid_for(plan, self._now()):
            raise MigrationOperationsError("migration_grant_invalid")
        try:
            self._execution_journal.register_grant(self._execution_identity(plan, grant))
        except MigrationJournalError as exc:
            raise MigrationOperationsError(exc.code, category="conflict") from None
        return grant

    def issue_grant(
        self,
        plan: MigrationOperationPlan,
        *,
        grant_id: str,
        ttl_seconds: int = 60,
    ) -> MigrationExecutionGrant:
        """Create and persist a short-lived pp-server-backend grant."""

        if not isinstance(plan, MigrationOperationPlan):
            raise MigrationOperationsError("migration_plan_typed_required")
        ttl = _require_positive_int(ttl_seconds, "migration_grant_ttl_invalid")
        if ttl > MAX_SHORT_LIVED_TTL_SECONDS:
            raise MigrationOperationsError("migration_grant_ttl_excessive")
        now = self._now()
        expires_at = min(plan.expires_at, now + timedelta(seconds=ttl))
        if expires_at <= now:
            raise MigrationOperationsError("migration_plan_expired", category="stale")
        return self.register_grant(
            plan,
            MigrationExecutionGrant(
                plan_hash=plan.plan_hash,
                grant_id=grant_id,
                issued_at=now,
                expires_at=expires_at,
            ),
        )

    @staticmethod
    def _observation_key(
        plan: MigrationOperationPlan,
    ) -> ResolvedEndpointDeploymentPlan | TopologyDigest:
        return plan.resolved_topology or TopologyDigest(plan.topology_digest)

    def _validate_plan(self, plan: MigrationOperationPlan, now: datetime) -> tuple[str, ...]:
        if not isinstance(plan, MigrationOperationPlan):
            raise MigrationOperationsError("migration_plan_typed_required")
        if now >= plan.expires_at:
            return ("migration_plan_expired",)
        if not plan.observations.is_fresh(now, self._observation_max_age_seconds):
            return ("migration_observation_stale",)
        return ()

    def _check_deadline(self, plan: MigrationOperationPlan, grant: MigrationExecutionGrant) -> None:
        """Fail closed when either short-lived authorization expires."""

        now = self._now()
        if now >= plan.expires_at:
            raise MigrationOperationsError("migration_plan_expired", category="stale")
        if now >= grant.expires_at:
            raise MigrationOperationsError("migration_grant_expired", category="stale")

    @staticmethod
    def _preflight_reasons(observations: MigrationObservations) -> tuple[str, ...]:
        reasons: list[str] = []
        if observations.canonical.writer_endpoint_id != PP_SERVER_BACKEND:
            reasons.append("migration_canonical_writer_invalid")
        if not observations.canonical.backup_ready:
            reasons.append("migration_canonical_backup_not_ready")
        if not observations.runtime.legacy_active:
            reasons.append("migration_legacy_not_active")
        if observations.runtime.backend_active:
            reasons.append("migration_backend_already_active")
        if not observations.nodes.ready:
            reasons.append("migration_nodes_not_ready")
        if not observations.derived.promotion_ready:
            reasons.append("migration_derived_promotion_not_ready")
        return tuple(reasons)

    def preflight(self, plan: MigrationOperationPlan) -> MigrationPreflight:
        """Run read-only gates.  No mutable adapter method is called."""

        now = self._now()
        if not isinstance(plan, MigrationOperationPlan):
            raise MigrationOperationsError("migration_plan_typed_required")
        reasons = list(self._validate_plan(plan, now))
        observed_digest: str | None = None
        if not reasons:
            # Re-observe in preflight so a plan cannot be admitted on stale
            # evidence.  This is read-only and safe for dry-run callers.
            try:
                current = self._observation_adapter.observe(
                    # The observer contract only needs the topology to identify
                    # its safe state.  A plan carries the digest, not the raw
                    # topology; the adapter may use its installation label.
                    self._observation_key(plan),
                    plan.installation_ref,
                )
            except Exception as exc:  # noqa: BLE001 - adapter details stay private
                reasons.append(_reason_from_exception(exc, "migration_observation_unavailable"))
            else:
                if not isinstance(current, MigrationObservations):
                    reasons.append("migration_observation_typed_required")
                else:
                    observed_digest = current.digest
                    if not current.is_fresh(now, self._observation_max_age_seconds):
                        reasons.append("migration_observation_stale")
                    elif current.digest != plan.observation_digest:
                        reasons.append("migration_observation_drift")
                    reasons.extend(self._preflight_reasons(current))
        return MigrationPreflight(
            ok=not reasons,
            reason_codes=tuple(dict.fromkeys(reasons)),
            plan_hash=plan.plan_hash,
            observed_digest=observed_digest,
            checked_at=now,
        )

    @staticmethod
    def _record(
        phase: str, outcome: str, reason: str = "migration_phase_completed"
    ) -> MigrationPhaseRecord:
        return MigrationPhaseRecord(phase=phase, outcome=outcome, reason_code=reason)

    def _reject(
        self,
        plan: MigrationOperationPlan,
        reason: str,
        *,
        dry_run: bool = False,
        phases: Sequence[MigrationPhaseRecord] = (),
    ) -> MigrationApplyResult:
        return MigrationApplyResult(
            accepted=False,
            dry_run=dry_run,
            outcome="dry-run" if dry_run else "rejected",
            reason_code=reason,
            plan_hash=plan.plan_hash,
            phases=tuple(phases),
        )

    def apply(
        self,
        plan: MigrationOperationPlan,
        grant: MigrationExecutionGrant,
        dry_run: bool = False,
    ) -> MigrationApplyResult:
        """Apply one plan under the configured replay and fencing journal.

        Production pp-core composition supplies the canonical SQLite journal;
        tests and non-production callers may explicitly use the in-memory
        implementation.
        """

        if not isinstance(plan, MigrationOperationPlan):
            raise MigrationOperationsError("migration_plan_typed_required")
        if not isinstance(grant, MigrationExecutionGrant):
            raise MigrationOperationsError("migration_grant_typed_required")
        if not isinstance(dry_run, bool):
            raise MigrationOperationsError("migration_dry_run_flag_invalid")
        if dry_run:
            return self._apply_impl(plan, grant, dry_run=True)

        # A JSON ``to_dict`` projection is intentionally useful for
        # inspection/transport, but it is not executable.  Mutable phases need
        # the server-memory typed bindings that were checked at plan time.
        if plan.resolved_topology is None or plan.artifact_bundle is None:
            return self._reject(plan, "migration_plan_bindings_unavailable")
        if plan.resolved_topology.manifest_digest != plan.topology_digest:
            return self._reject(plan, "migration_plan_binding_mismatch")
        try:
            if _artifact_bundle_digest(plan.artifact_bundle) != plan.artifact_bundle_digest:
                return self._reject(plan, "migration_plan_binding_mismatch")
            _validate_artifact_topology_compatibility(plan.resolved_topology, plan.artifact_bundle)
        except Exception:  # noqa: BLE001 - typed binding must fail closed
            return self._reject(plan, "migration_plan_binding_mismatch")

        return self._apply_impl(plan, grant, dry_run=False)

    def _apply_impl(
        self,
        plan: MigrationOperationPlan,
        grant: MigrationExecutionGrant,
        dry_run: bool = False,
    ) -> MigrationApplyResult:
        """Apply fixed phases after fresh admission and drift checks."""

        if not isinstance(plan, MigrationOperationPlan):
            raise MigrationOperationsError("migration_plan_typed_required")
        if not isinstance(grant, MigrationExecutionGrant):
            raise MigrationOperationsError("migration_grant_typed_required")
        if not isinstance(dry_run, bool):
            raise MigrationOperationsError("migration_dry_run_flag_invalid")
        now = self._now()
        if not grant.is_valid_for(plan, now):
            return self._reject(plan, "migration_grant_invalid", dry_run=dry_run)

        preflight = self.preflight(plan)
        if not preflight.ok:
            return self._reject(
                plan,
                preflight.reason_codes[0]
                if preflight.reason_codes
                else "migration_preflight_rejected",
                dry_run=dry_run,
            )
        if dry_run:
            return MigrationApplyResult(
                accepted=True,
                dry_run=True,
                outcome="dry-run",
                reason_code="migration_dry_run_ready",
                plan_hash=plan.plan_hash,
                phases=tuple(
                    self._record(phase, "skipped", "migration_dry_run") for phase in APPLY_PHASES
                ),
            )

        # The preflight observation is not sufficient authorization.  Take one
        # final read immediately before the first mutable phase and reject any
        # state drift without touching a mutable adapter.
        try:
            current = self._observation_adapter.observe(
                self._observation_key(plan), plan.installation_ref
            )
        except Exception as exc:  # noqa: BLE001 - adapter details stay private
            return self._reject(
                plan, _reason_from_exception(exc, "migration_observation_unavailable")
            )
        if not isinstance(current, MigrationObservations):
            return self._reject(plan, "migration_observation_typed_required")
        if not current.is_fresh(self._now(), self._observation_max_age_seconds):
            return self._reject(plan, "migration_observation_stale")
        if current.digest != plan.observation_digest:
            return self._reject(plan, "migration_observation_drift")

        try:
            lease = self._execution_journal.begin(
                self._execution_identity(plan, grant),
                now=self._now(),
                lease_expires_at=self._now() + timedelta(seconds=self._migration_lease_seconds),
            )
        except MigrationJournalError as exc:
            return self._reject(plan, exc.code)

        context = MigrationExecutionContext(plan=plan, lease=lease)
        try:
            result = self._execute_phases(context, grant)
            self._execution_journal.complete(
                lease,
                outcome=result.outcome,
                receipt=result.to_dict(),
                now=self._now(),
            )
        except MigrationJournalError as exc:
            category = "conflict" if exc.code == "migration_operation_fence_lost" else "unavailable"
            raise MigrationOperationsError(exc.code, category=category) from None
        return result

    def _check_execution(
        self, context: MigrationExecutionContext, grant: MigrationExecutionGrant
    ) -> None:
        self._check_deadline(context.plan, grant)
        self._execution_journal.assert_current(context.lease, now=self._now())

    def _run_phase(
        self,
        context: MigrationExecutionContext,
        grant: MigrationExecutionGrant,
        action: Callable[[MigrationExecutionContext], object],
    ) -> object:
        self._check_execution(context, grant)
        result = action(context)
        self._execution_journal.assert_current(context.lease, now=self._now())
        return result

    def _record_phase(
        self,
        context: MigrationExecutionContext,
        *,
        phase_index: int,
        record: MigrationPhaseRecord,
    ) -> None:
        """Persist one ordered phase outcome behind the current fence."""

        self._execution_journal.record_phase(
            context.lease,
            phase_index=phase_index,
            phase=record.phase,
            outcome=record.outcome,
            reason_code=record.reason_code,
            now=self._now(),
        )

    def _execute_phases(
        self, context: MigrationExecutionContext, grant: MigrationExecutionGrant
    ) -> MigrationApplyResult:
        plan = context.plan
        phases: list[MigrationPhaseRecord] = []
        cutover_started = False
        canonical_migration_completed = False
        backup_receipt: CanonicalBackupMigrationReceipt | None = None
        schema_receipt: CollaborationSchemaInstallReceipt | None = None
        try:
            self._run_phase(context, grant, self._adapters.edge_compute.stage_and_verify)
            record = self._record(PHASE_STAGE_EDGE_COMPUTE, "completed")
            phases.append(record)
            self._record_phase(context, phase_index=0, record=record)

            self._run_phase(context, grant, self._adapters.canonical_state.rehearse)
            record = self._record(PHASE_CANONICAL_REHEARSAL, "completed")
            phases.append(record)
            self._record_phase(context, phase_index=1, record=record)

            cutover_started = True
            self._run_phase(context, grant, self._adapters.runtime.stop_legacy)
            record = self._record(PHASE_STOP_LEGACY, "completed")
            phases.append(record)
            self._record_phase(context, phase_index=2, record=record)

            backup_result = self._run_phase(
                context,
                grant,
                self._adapters.canonical_state.backup_and_migrate,
            )
            if not isinstance(backup_result, CanonicalBackupMigrationReceipt):
                raise MigrationOperationsError("migration_canonical_backup_receipt_required")
            backup_result.validate_for(context)
            backup_receipt = backup_result
            record = self._record(PHASE_CANONICAL_BACKUP_MIGRATION, "completed")
            phases.append(record)
            self._record_phase(context, phase_index=3, record=record)
            canonical_migration_completed = True

            schema_result = self._run_phase(
                context,
                grant,
                lambda execution_context: self._adapters.collaboration_schema.install(
                    execution_context,
                    backup_result,
                ),
            )
            if not isinstance(schema_result, CollaborationSchemaInstallReceipt):
                raise MigrationOperationsError("migration_collaboration_schema_receipt_required")
            schema_result.validate_for(context, backup_result)
            schema_receipt = schema_result
            record = self._record(PHASE_COLLABORATION_SCHEMA_INSTALL, "completed")
            phases.append(record)
            self._record_phase(context, phase_index=4, record=record)

            self._run_phase(context, grant, self._adapters.runtime.start_backend)
            record = self._record(PHASE_START_BACKEND, "completed")
            phases.append(record)
            self._record_phase(context, phase_index=5, record=record)

            self._run_phase(
                context, grant, self._adapters.derived_index.shadow_rebuild_verify_promote
            )
            record = self._record(PHASE_SHADOW_REBUILD_PROMOTE, "completed")
            phases.append(record)
            self._record_phase(context, phase_index=6, record=record)

            self._run_phase(context, grant, self._adapters.maintenance.enable)
            record = self._record(PHASE_ENABLE_MAINTENANCE, "completed")
            phases.append(record)
            self._record_phase(context, phase_index=7, record=record)

            self._run_phase(context, grant, self._adapters.retention_cache.apply)
            record = self._record(PHASE_RETENTION_CACHE_POLICY, "completed")
            phases.append(record)
            self._record_phase(context, phase_index=8, record=record)
        except MigrationJournalError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter errors are sanitized below
            reason = _reason_from_exception(exc, "migration_phase_failed")
            failed_phase = (
                APPLY_PHASES[len(phases)] if len(phases) < len(APPLY_PHASES) else APPLY_PHASES[-1]
            )
            failed_record = self._record(failed_phase, "failed", reason)
            phases.append(failed_record)
            self._record_phase(context, phase_index=len(phases) - 1, record=failed_record)
            if not cutover_started:
                return MigrationApplyResult(
                    accepted=False,
                    dry_run=False,
                    outcome="rejected",
                    reason_code=reason,
                    plan_hash=plan.plan_hash,
                    phases=tuple(phases),
                    canonical_backup_receipt_sha256=(
                        "" if backup_receipt is None else backup_receipt.receipt_sha256
                    ),
                    collaboration_schema_receipt_sha256=(
                        "" if schema_receipt is None else schema_receipt.receipt_sha256
                    ),
                )
            rollback_phases, rollback_errors = self._rollback(
                context, canonical_migration_completed=canonical_migration_completed
            )
            phases.extend(rollback_phases)
            return MigrationApplyResult(
                accepted=False,
                dry_run=False,
                outcome="recovery-required" if rollback_errors else "rolled-back",
                reason_code=reason,
                plan_hash=plan.plan_hash,
                phases=tuple(phases),
                rollback_attempted=True,
                rollback_completed=not rollback_errors,
                rollback_reason_codes=tuple(rollback_errors),
                canonical_backup_receipt_sha256=(
                    "" if backup_receipt is None else backup_receipt.receipt_sha256
                ),
                collaboration_schema_receipt_sha256=(
                    "" if schema_receipt is None else schema_receipt.receipt_sha256
                ),
            )

        return MigrationApplyResult(
            accepted=True,
            dry_run=False,
            outcome="applied",
            reason_code="migration_applied",
            plan_hash=plan.plan_hash,
            phases=tuple(phases),
            canonical_backup_receipt_sha256=(
                "" if backup_receipt is None else backup_receipt.receipt_sha256
            ),
            collaboration_schema_receipt_sha256=(
                "" if schema_receipt is None else schema_receipt.receipt_sha256
            ),
        )

    def _rollback(
        self,
        context: MigrationExecutionContext,
        *,
        canonical_migration_completed: bool,
    ) -> tuple[list[MigrationPhaseRecord], list[str]]:
        """Perform bounded rollback in a fixed, best-effort order."""

        actions: dict[str, Callable[[], None]] = {
            "disable-maintenance": lambda: self._adapters.maintenance.disable(context),
            "revert-derived-selection": lambda: self._adapters.derived_index.revert_selection(
                context
            ),
            "stop-new-backend": lambda: self._adapters.runtime.stop_backend(context),
            "canonical-restore": lambda: self._adapters.canonical_state.restore(context),
            "restart-legacy": lambda: self._adapters.runtime.restart_legacy(context),
        }
        records: list[MigrationPhaseRecord] = []
        errors: list[str] = []
        for phase_index, phase in enumerate(ROLLBACK_PHASES, start=len(APPLY_PHASES)):
            action = actions[phase]
            if phase == "canonical-restore" and not canonical_migration_completed:
                record = self._record(
                    phase,
                    "skipped",
                    "migration_canonical_restore_not_needed",
                )
                records.append(record)
                self._record_phase(context, phase_index=phase_index, record=record)
                continue
            try:
                self._execution_journal.assert_current(context.lease, now=self._now())
                action()
            except Exception as exc:  # noqa: BLE001 - continue bounded rollback
                reason = _reason_from_exception(exc, "migration_rollback_phase_failed")
                errors.append(reason)
                record = self._record(phase, "failed", reason)
            else:
                record = self._record(phase, "completed")
            records.append(record)
            self._record_phase(context, phase_index=phase_index, record=record)
        return records, errors


def _reason_from_exception(exc: Exception, fallback: str) -> str:
    # Only codes from this public boundary are safe to relay.  An arbitrary
    # adapter object may expose a ``code`` attribute containing internal
    # taxonomy, paths, or provider details.
    if isinstance(
        exc,
        (MigrationOperationsError, MigrationJournalError, CollaborationSchemaMigrationError),
    ) and _SAFE_CODE.fullmatch(exc.code):
        return exc.code
    # Do not expose exception text: it may contain paths, addresses, or
    # credentials supplied by an adapter implementation.
    return fallback


__all__ = [
    "APPLY_PHASES",
    "DEFAULT_OBSERVATION_MAX_AGE_SECONDS",
    "DEFAULT_PLAN_TTL_SECONDS",
    "MAX_SHORT_LIVED_TTL_SECONDS",
    "DerivedGenerationObservation",
    "EdgeComputeMigrationAdapter",
    "ExecutionGrant",
    "CanonicalStateMigrationAdapter",
    "CollaborationSchemaMigrationAdapter",
    "CanonicalStateObservation",
    "DerivedIndexMigrationAdapter",
    "MaintenanceMigrationAdapter",
    "MigrationAdapters",
    "MigrationApplyResult",
    "MigrationExecutionContext",
    "MigrationExecutionGrant",
    "MigrationGrant",
    "MigrationIntent",
    "MigrationObservations",
    "MigrationOperationPlan",
    "MigrationPlan",
    "MigrationOperations",
    "MigrationOperationsError",
    "MigrationPhaseRecord",
    "MigrationPreflight",
    "MigrationObservationAdapter",
    "NodeReadinessObservation",
    "OPERATION_PHASE_MANIFEST",
    "ROLLBACK_PHASES",
    "RetentionCacheMigrationAdapter",
    "RuntimeMigrationAdapter",
    "RuntimeObservation",
    "TopologyDigest",
]
