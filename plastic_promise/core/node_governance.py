"""Server-owned governance for non-canonical inference nodes.

The module has two deliberately narrow seams:

* :class:`NodeGovernanceStore` is the registry for verified node identity,
  health, capacity, reservations and latency evidence.
* :class:`NodeInferenceWorkCoordinator` maps an already-authorized canonical
  reference to the existing :class:`~plastic_promise.core.derived_work.DerivedWorkStore`.

The latter is important: node governance does *not* create a second durable
task queue.  SQLite's existing derived-work outbox remains the source of truth
for idempotency, leases, retries and reconciliation.  Nodes receive only an
opaque server lease and have no canonical-memory write authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import sqlite3
import statistics
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from typing import Protocol, cast

from plastic_promise.core.derived_work import (
    DerivedWorkCreateResult,
    DerivedWorkLease,
    DerivedWorkStore,
)
from plastic_promise.core.node_governance_schema import node_governance_schema_present
from plastic_promise.core.paths import get_db_path
from plastic_promise.deployment.manifest import ResolvedDeployment

_IDENTIFIER_RE = re.compile(r"\A[a-z][a-z0-9_.:-]{1,127}\Z")
_CONTROL_REVISION_RE = re.compile(r"\Acfg-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\Z")
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_REVISION_RE = re.compile(r"\A(?:[0-9a-f]{40}|sha256:[0-9a-f]{64})\Z")
_FAILURE_CODE_RE = re.compile(r"\A[a-z][a-z0-9_.:-]{1,96}\Z")
_NODE_KINDS = frozenset({"remote-node", "ollama", "cloud"})
_POLICIES = frozenset(
    {
        "remote-node-first",
        "ollama-first",
        "fastest-estimated",
        "pinned-node",
        "cloud-only",
    }
)
ACCELERATOR_TASK_KINDS = frozenset(
    {
        "embedding-reconcile",
        "vector-relations",
        "semantic-dedupe",
        "conflict-risk",
        "preclassification",
        "scoring-evidence",
    }
)
_DEFAULT_HEALTH_TTL_SECONDS = 5 * 60
_DEFAULT_LEASE_SECONDS = 60
_MAX_LEASE_SECONDS = 15 * 60
_FASTEST_MIN_SUCCESS_SAMPLES = 20
_SERVER_STORE_TOKEN = object()
_SERVER_RECEIPT_TOKEN = object()
_IDENTITY_DRIFT_FAILURE_CODES = frozenset(
    {
        "node_private_embedding_identity_drift",
        "node_private_embedding_dimension_invalid",
        "node_private_rerank_identity_drift",
        "node_private_rerank_result_identity_drift",
        "node_private_structured_json_identity_drift",
        "node_private_structured_json_result_identity_drift",
    }
)
_LOCAL_REQUEST_FAILURE_CODES = frozenset(
    {
        "node_private_embedding_lease_invalid",
        "node_private_embedding_input_invalid",
        "node_private_embedding_input_too_large",
        "node_private_rerank_lease_invalid",
        "node_private_rerank_input_invalid",
        "node_private_structured_json_lease_invalid",
        "node_private_structured_json_input_invalid",
    }
)
_OVERLOAD_DEFERRED_FAILURE_CODES = frozenset({"node_overloaded", "node_resource_busy"})
_GENERIC_NODE_EXECUTION_FAILURE_CODES = frozenset(
    {
        "node_execution_failed",
        "node_execution_result_invalid",
        "node_index_embedding_failed",
    }
)
_DERIVED_JOB_KIND = "node-inference"
_FOREGROUND_MARKER_KIND = "node-inference-foreground"
_ACCELERATOR_DERIVED_JOB_KIND = "accelerator-max"


class NodeGovernanceError(RuntimeError):
    """A stable, non-sensitive node-governance error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def is_accelerator_task_kind(task_kind: object) -> bool:
    """Return whether a task kind is allowed for non-canonical accelerator work."""

    return isinstance(task_kind, str) and task_kind in ACCELERATOR_TASK_KINDS


@dataclass(frozen=True)
class OperationPolicy:
    """All operation-specific routing decisions in one immutable policy."""

    capability: str
    priority: int
    fallback_chain: tuple[str, ...]
    identity_kind: str | None


_OPERATION_POLICIES: Mapping[str, OperationPolicy] = {
    "embedding": OperationPolicy(
        capability="embedding",
        priority=300,
        # The server never invokes a provider directly.  Every inference
        # attempt is scheduled through a registered compute node; ``defer``
        # is the only safe server-side degradation.
        fallback_chain=("registered-compute-node", "defer"),
        identity_kind="embedding",
    ),
    "rerank": OperationPolicy(
        capability="rerank",
        priority=200,
        fallback_chain=("registered-compute-node", "original-order"),
        identity_kind="rerank",
    ),
    "structured-json": OperationPolicy(
        capability="structured-json",
        priority=100,
        fallback_chain=("registered-compute-node", "defer"),
        identity_kind="structured-json",
    ),
}


@dataclass(frozen=True)
class NodeIdentityEvidence:
    """Pinned model identity independently observed by the server."""

    protocol_version: str
    embedding_model: str
    embedding_revision: str
    embedding_dimension: int
    embedding_normalization: str
    embedding_artifact_sha256: str
    rerank_model: str
    rerank_revision: str
    rerank_artifact_sha256: str
    # Optional fields keep v1 registrations readable while allowing a node
    # to expose the hosted/local structured-json adapter it owns.
    provider_class: str = "local"
    structured_json_model: str | None = None
    structured_json_revision: str | None = None

    def __post_init__(self) -> None:
        _safe_identity(self.protocol_version, "node_protocol_version_invalid")
        _safe_identity(self.embedding_model, "node_embedding_model_invalid")
        _pinned_revision(self.embedding_revision, "node_embedding_revision_invalid")
        _safe_identity(self.embedding_normalization, "node_embedding_normalization_invalid")
        _digest(self.embedding_artifact_sha256, "node_embedding_artifact_sha256_invalid")
        _safe_identity(self.rerank_model, "node_rerank_model_invalid")
        _pinned_revision(self.rerank_revision, "node_rerank_revision_invalid")
        _digest(self.rerank_artifact_sha256, "node_rerank_artifact_sha256_invalid")
        if self.provider_class not in {"local", "cloud", "hybrid"}:
            raise NodeGovernanceError("node_provider_class_invalid")
        if (self.structured_json_model is None) != (self.structured_json_revision is None):
            raise NodeGovernanceError("node_structured_json_identity_incomplete")
        if self.structured_json_model is not None:
            _safe_identity(self.structured_json_model, "node_structured_json_model_invalid")
            _pinned_revision(
                self.structured_json_revision,
                "node_structured_json_revision_invalid",
            )
        if (
            not isinstance(self.embedding_dimension, int)
            or isinstance(self.embedding_dimension, bool)
            or not 1 <= self.embedding_dimension <= 65_536
        ):
            raise NodeGovernanceError("node_embedding_dimension_invalid")

    @property
    def embedding_key(self) -> str:
        return _identity_digest(
            {
                "model": self.embedding_model,
                "revision": self.embedding_revision,
                "dimension": self.embedding_dimension,
                "normalization": self.embedding_normalization,
                "artifact_sha256": self.embedding_artifact_sha256,
            }
        )

    @property
    def rerank_key(self) -> str:
        return _identity_digest(
            {
                "model": self.rerank_model,
                "revision": self.rerank_revision,
                "artifact_sha256": self.rerank_artifact_sha256,
            }
        )

    @property
    def structured_json_key(self) -> str | None:
        if self.structured_json_model is None or self.structured_json_revision is None:
            return None
        return _identity_digest(
            {
                "model": self.structured_json_model,
                "revision": self.structured_json_revision,
            }
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "protocol_version": self.protocol_version,
            "provider_class": self.provider_class,
            "embedding": {
                "model": self.embedding_model,
                "revision": self.embedding_revision,
                "dimension": self.embedding_dimension,
                "normalization": self.embedding_normalization,
                "artifact_sha256": self.embedding_artifact_sha256,
                "identity": self.embedding_key,
            },
            "rerank": {
                "model": self.rerank_model,
                "revision": self.rerank_revision,
                "artifact_sha256": self.rerank_artifact_sha256,
                "identity": self.rerank_key,
            },
        }
        if self.structured_json_key is not None:
            assert self.structured_json_model is not None
            assert self.structured_json_revision is not None
            value["structured_json"] = {
                "model": self.structured_json_model,
                "revision": self.structured_json_revision,
                "identity": self.structured_json_key,
            }
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> NodeIdentityEvidence:
        try:
            embedding = _mapping(value.get("embedding"))
            rerank = _mapping(value.get("rerank"))
            structured = value.get("structured_json")
            structured_mapping = _mapping(structured) if structured is not None else {}
            return cls(
                protocol_version=_string(
                    value.get("protocol_version"), "node_protocol_version_invalid"
                ),
                embedding_model=_string(embedding.get("model"), "node_embedding_model_invalid"),
                embedding_revision=_string(
                    embedding.get("revision"), "node_embedding_revision_invalid"
                ),
                embedding_dimension=cast("int", embedding.get("dimension")),
                embedding_normalization=_string(
                    embedding.get("normalization"), "node_embedding_normalization_invalid"
                ),
                embedding_artifact_sha256=_string(
                    embedding.get("artifact_sha256"), "node_embedding_artifact_sha256_invalid"
                ),
                rerank_model=_string(rerank.get("model"), "node_rerank_model_invalid"),
                rerank_revision=_string(rerank.get("revision"), "node_rerank_revision_invalid"),
                rerank_artifact_sha256=_string(
                    rerank.get("artifact_sha256"), "node_rerank_artifact_sha256_invalid"
                ),
                provider_class=_string(value.get("provider_class", "local"), "node_provider_class_invalid"),
                structured_json_model=(
                    _string(
                        structured_mapping.get("model"),
                        "node_structured_json_model_invalid",
                    )
                    if structured is not None
                    else None
                ),
                structured_json_revision=(
                    _string(
                        structured_mapping.get("revision"),
                        "node_structured_json_revision_invalid",
                    )
                    if structured is not None
                    else None
                ),
            )
        except AttributeError as exc:
            raise NodeGovernanceError("node_identity_evidence_invalid") from exc


@dataclass(frozen=True)
class NodeRegistration:
    """Non-secret declaration that a server has not yet trusted by itself."""

    node_id: str
    node_kind: str
    transport_id: str
    transport_evidence: str
    expected_identity: NodeIdentityEvidence
    capabilities: tuple[str, ...]
    max_concurrency: int

    def __post_init__(self) -> None:
        _identifier(self.node_id, "node_id_invalid")
        if self.node_kind not in _NODE_KINDS:
            raise NodeGovernanceError("node_kind_invalid")
        _identifier(self.transport_id, "node_transport_id_invalid")
        _digest(self.transport_evidence, "node_transport_evidence_invalid")
        if not isinstance(self.expected_identity, NodeIdentityEvidence):
            raise NodeGovernanceError("node_expected_identity_invalid")
        _capabilities(self.capabilities)
        _positive_int(self.max_concurrency, "node_max_concurrency_invalid")


@dataclass(frozen=True)
class NodeHealthEvidence:
    """Authenticated health observation made through the server's private path."""

    node_id: str
    observed_identity: NodeIdentityEvidence
    capabilities: tuple[str, ...]
    queue_depth: int
    available_slots: int

    def __post_init__(self) -> None:
        _identifier(self.node_id, "node_id_invalid")
        if not isinstance(self.observed_identity, NodeIdentityEvidence):
            raise NodeGovernanceError("node_identity_evidence_invalid")
        _capabilities(self.capabilities)
        for value, code in (
            (self.queue_depth, "node_queue_depth_invalid"),
            (self.available_slots, "node_available_slots_invalid"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise NodeGovernanceError(code)


@dataclass(frozen=True)
class NodeSnapshot:
    """Bounded server observation used for scheduling and diagnostics."""

    node_id: str
    node_kind: str
    transport_id: str
    transport_evidence: str
    expected_identity: NodeIdentityEvidence
    observed_identity: NodeIdentityEvidence | None
    declared_capabilities: tuple[str, ...]
    observed_capabilities: tuple[str, ...]
    max_concurrency: int
    queue_depth: int
    available_slots: int
    active_lease_count: int
    registration_source: str
    registration_reference: str
    state: str
    quarantine_reason: str | None
    last_health_at: str | None


@dataclass(frozen=True)
class NodeVerificationReceipt:
    """Unforgeable-in-process record issued only after server verification."""

    source: str
    reference: str
    evidence_digest: str
    issued_at: str
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _SERVER_RECEIPT_TOKEN:
            raise NodeGovernanceError("node_verification_receipt_server_required")
        if self.source not in {"deployment", "controlled-revision"}:
            raise NodeGovernanceError("node_registration_source_invalid")
        _registration_reference(self.source, self.reference)
        _digest(self.evidence_digest, "node_verification_receipt_invalid")


@dataclass(frozen=True)
class NodeIdentityRevalidationReceipt:
    """Durable, secret-free proof that a node matched one active profile."""

    receipt_id: str
    node_id: str
    config_revision: str
    required_identity: str
    observed_identity: str
    profile_digest: str
    issued_at: str

    def __post_init__(self) -> None:
        _identifier(self.receipt_id, "node_identity_receipt_id_invalid")
        _identifier(self.node_id, "node_id_invalid")
        _controlled_revision(self.config_revision, "node_identity_receipt_revision_invalid")
        _digest(self.required_identity, "node_identity_receipt_required_identity_invalid")
        _digest(self.observed_identity, "node_identity_receipt_observed_identity_invalid")
        _digest(self.profile_digest, "node_identity_receipt_profile_digest_invalid")
        _parse_utc(self.issued_at)


@dataclass(frozen=True)
class VerifiedNodeRegistration:
    """One server-issued receipt paired with exactly one registration."""

    registration: NodeRegistration
    receipt: NodeVerificationReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.registration, NodeRegistration):
            raise NodeGovernanceError("node_registration_invalid")
        if not isinstance(self.receipt, NodeVerificationReceipt):
            raise NodeGovernanceError("node_verification_receipt_invalid")


class ControlledRevisionStore(Protocol):
    """The small control-plane seam needed to validate a persisted revision."""

    def get_revision(self, revision_id: str) -> object: ...

    def safe_config(self) -> object: ...


class PrivateNodeTransportVerifier(Protocol):
    """Server-private adapter that returns independently probed node evidence."""

    def probe(self, registration: NodeRegistration) -> object: ...


class NodeRegistrationAuthority:
    """Server-only authority that issues verified registration receipts.

    Nodes and dashboards can submit a declaration and authenticated health
    evidence, but neither can manufacture a receipt.  Registration storage
    accepts the receipt object—not an arbitrary digest or revision string.
    """

    def __init__(self, *, clock: Callable[[], datetime], _token: object | None = None) -> None:
        if _token is not _SERVER_STORE_TOKEN:
            raise NodeGovernanceError("node_registration_authority_server_required")
        self._clock = clock

    def verify_deployment(
        self,
        deployment: ResolvedDeployment,
        registration: NodeRegistration,
        health: NodeHealthEvidence,
    ) -> VerifiedNodeRegistration:
        if not isinstance(deployment, ResolvedDeployment):
            raise NodeGovernanceError("node_deployment_resolution_invalid")
        if deployment.profile_id != "split-accelerated":
            raise NodeGovernanceError("node_deployment_profile_incompatible")
        if (
            registration.node_kind != "remote-node"
            or registration.node_id not in deployment.node_ids
        ):
            raise NodeGovernanceError("node_not_declared_in_deployment")
        self._require_matching_health(registration, health)
        return VerifiedNodeRegistration(
            registration,
            self._receipt("deployment", deployment.deployment_id, registration, health),
        )

    def verify_controlled_revision(
        self,
        control_store: ControlledRevisionStore,
        *,
        config_revision: str,
        registration: NodeRegistration,
        health: NodeHealthEvidence,
    ) -> VerifiedNodeRegistration:
        _controlled_revision(config_revision, "node_config_revision_invalid")
        # Protocol runtime checks are unavailable; explicitly require the two
        # methods needed to prove a persisted, active revision.
        if not callable(getattr(control_store, "get_revision", None)) or not callable(
            getattr(control_store, "safe_config", None)
        ):
            raise NodeGovernanceError("node_controlled_revision_store_invalid")
        try:
            revision = control_store.get_revision(config_revision)
            active = control_store.safe_config()
        except Exception as exc:
            raise NodeGovernanceError("node_controlled_revision_unavailable") from exc
        if str(getattr(revision, "revision_id", "")) != config_revision:
            raise NodeGovernanceError("node_controlled_revision_not_persisted")
        active_id = getattr(active, "revision_id", getattr(active, "active_revision_id", None))
        if str(active_id or "") != config_revision:
            raise NodeGovernanceError("node_controlled_revision_not_active")
        self._require_matching_health(registration, health)
        return VerifiedNodeRegistration(
            registration,
            self._receipt("controlled-revision", config_revision, registration, health),
        )

    def verify_private_transport(
        self,
        control_store: ControlledRevisionStore,
        *,
        config_revision: str,
        registration: NodeRegistration,
        transport: PrivateNodeTransportVerifier,
    ) -> VerifiedNodeRegistration:
        """Register only evidence independently probed through a private path.

        The caller's registration declaration intentionally has no authority to
        choose the observed identity or transport proof.  The transport adapter
        owns endpoint resolution and returns a replacement registration carrying
        a server-derived evidence digest plus a fresh health observation.
        """

        if not callable(getattr(transport, "probe", None)):
            raise NodeGovernanceError("node_private_transport_verifier_invalid")
        try:
            observation = transport.probe(registration)
        except NodeGovernanceError:
            raise
        except Exception as exc:
            raise NodeGovernanceError("node_private_transport_unavailable") from exc
        probed_registration = getattr(observation, "registration", None)
        health = getattr(observation, "health", None)
        if not isinstance(probed_registration, NodeRegistration) or not isinstance(
            health, NodeHealthEvidence
        ):
            raise NodeGovernanceError("node_private_transport_evidence_invalid")
        if (
            probed_registration.node_id != registration.node_id
            or probed_registration.node_kind != registration.node_kind
            or probed_registration.transport_id != registration.transport_id
            or probed_registration.expected_identity != registration.expected_identity
            or probed_registration.capabilities != registration.capabilities
            or probed_registration.max_concurrency != registration.max_concurrency
        ):
            raise NodeGovernanceError("node_private_transport_binding_invalid")
        return self.verify_controlled_revision(
            control_store,
            config_revision=config_revision,
            registration=probed_registration,
            health=health,
        )

    def _require_matching_health(
        self, registration: NodeRegistration, health: NodeHealthEvidence
    ) -> None:
        if registration.node_id != health.node_id:
            raise NodeGovernanceError("node_verification_node_mismatch")
        if registration.expected_identity != health.observed_identity:
            raise NodeGovernanceError("node_verification_identity_mismatch")
        if not set(health.capabilities).issubset(registration.capabilities):
            raise NodeGovernanceError("node_health_capability_unregistered")

    def _receipt(
        self,
        source: str,
        reference: str,
        registration: NodeRegistration,
        health: NodeHealthEvidence,
    ) -> NodeVerificationReceipt:
        evidence = {
            "source": source,
            "reference": reference,
            "registration": {
                "node_id": registration.node_id,
                "node_kind": registration.node_kind,
                "transport_id": registration.transport_id,
                "transport_evidence": registration.transport_evidence,
                "expected_identity": registration.expected_identity.to_dict(),
                "capabilities": list(registration.capabilities),
                "max_concurrency": registration.max_concurrency,
            },
            "health": {
                "identity": health.observed_identity.to_dict(),
                "capabilities": list(health.capabilities),
                "queue_depth": health.queue_depth,
                "available_slots": health.available_slots,
            },
        }
        return NodeVerificationReceipt(
            source=source,
            reference=reference,
            evidence_digest=_identity_digest(evidence),
            issued_at=_utc_text(_require_clock(self._clock)),
            _token=_SERVER_RECEIPT_TOKEN,
        )


@dataclass(frozen=True)
class NodeTaskRequest:
    """Untrusted task envelope; it cannot choose a policy, pin or identity."""

    project_id: str
    idempotency_key: str
    operation: str
    input_reference: str

    def __post_init__(self) -> None:
        _identifier(self.project_id, "node_task_project_id_invalid")
        _identifier(self.idempotency_key, "node_task_idempotency_key_invalid")
        _operation_policy(self.operation)
        _identifier(self.input_reference, "node_task_input_reference_invalid")


@dataclass(frozen=True)
class ResolvedNodeTask:
    """Server-controlled, project-owned canonical reference and route policy."""

    project_id: str
    operation: str
    input_reference: str
    subject_hash: str
    visibility: str
    config_revision: str
    required_identity: str
    scheduling_policy: str
    inference_mode: str = "hybrid"
    pinned_node_id: str | None = None
    allowed_node_ids: tuple[str, ...] = ()
    # A production control revision may bind node admission to the exact
    # private compute profile that was activated.  Legacy/library fixtures
    # leave this unset and retain their deterministic test-only admission.
    profile_digest: str | None = None
    max_attempts: int = 4

    def __post_init__(self) -> None:
        _identifier(self.project_id, "node_task_project_id_invalid")
        _operation_policy(self.operation)
        _identifier(self.input_reference, "node_task_input_reference_invalid")
        _digest(self.subject_hash, "node_task_subject_hash_invalid")
        if self.visibility not in {"private", "project", "shared", "global"}:
            raise NodeGovernanceError("node_task_visibility_invalid")
        _controlled_revision(self.config_revision, "node_task_config_revision_invalid")
        _digest(self.required_identity, "node_task_required_identity_invalid")
        if self.scheduling_policy not in _POLICIES:
            raise NodeGovernanceError("node_task_policy_invalid")
        if self.inference_mode not in {"local", "cloud", "hybrid"}:
            raise NodeGovernanceError("node_task_inference_mode_invalid")
        if self.scheduling_policy == "pinned-node" and self.pinned_node_id is None:
            raise NodeGovernanceError("node_task_pinned_node_required")
        if self.pinned_node_id is not None:
            _identifier(self.pinned_node_id, "node_task_pinned_node_invalid")
        if any(
            _identifier(node_id, "node_task_allowed_node_invalid") != node_id
            for node_id in self.allowed_node_ids
        ):
            raise NodeGovernanceError("node_task_allowed_node_invalid")
        if len(set(self.allowed_node_ids)) != len(self.allowed_node_ids):
            raise NodeGovernanceError("node_task_allowed_node_invalid")
        if self.profile_digest is not None:
            _digest(self.profile_digest, "node_task_profile_digest_invalid")
        if not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 32:
            raise NodeGovernanceError("node_task_max_attempts_invalid")


class NodeTaskAuthority(Protocol):
    """Server-owned canonical resolver used at enqueue and before execution."""

    def resolve(self, request: NodeTaskRequest) -> ResolvedNodeTask: ...

    def verify(self, resolved: ResolvedNodeTask) -> None: ...

    def verify_lease(self, resolved: ResolvedNodeTask) -> None: ...


@dataclass(frozen=True)
class NodeSelection:
    node: NodeSnapshot
    reason: str


@dataclass(frozen=True)
class NodeWorkLease:
    """A verified binding for durable or foreground-derived work.

    Foreground inference keeps raw input process-local while carrying the
    secret-free marker lease and fence.  The heartbeat callback renews both
    the durable marker and its node reservation without exposing source data.
    """

    derived_lease: DerivedWorkLease | None
    resolved: ResolvedNodeTask
    node_id: str
    transport_id: str
    selection_reason: str
    _heartbeat: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def heartbeat(self) -> None:
        """Renew this work capability or fail closed after fence invalidation."""

        if self._heartbeat is None:
            raise NodeExecutionFailure("node_work_heartbeat_unavailable")
        self._heartbeat()


@dataclass(frozen=True)
class NodeExecutionResult:
    """Small verified execution evidence; inference payload stays server-side."""

    latency_ms: float | int
    evidence: Mapping[str, object] = field(default_factory=dict)
    result: Mapping[str, object] = field(default_factory=dict)


class NodeExecutionFailure(RuntimeError):
    """An adapter failure carrying only a stable retry reason."""

    def __init__(self, code: str) -> None:
        if _FAILURE_CODE_RE.fullmatch(code) is None:
            raise NodeGovernanceError("node_task_failure_code_invalid")
        self.code = code
        super().__init__(code)


class _ForegroundLeaseHeartbeat:
    """Keep one foreground marker and reservation alive during blocking work."""

    def __init__(self, renew: Callable[[], None], *, lease_seconds: int) -> None:
        self._renew = renew
        self._interval_seconds = max(0.05, min(float(lease_seconds) / 3.0, 5.0))
        self._stop = Event()
        self._invalidated = Event()
        self._pulse_lock = Lock()
        self._thread = Thread(
            target=self._run,
            name="pp-foreground-lease-heartbeat",
            daemon=True,
        )

    @property
    def invalidated(self) -> bool:
        return self._invalidated.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and current_thread() is not self._thread:
            self._thread.join()

    def pulse(self) -> None:
        with self._pulse_lock:
            if self.invalidated:
                raise NodeExecutionFailure("node_foreground_lease_invalidated")
            try:
                self._renew()
            except Exception as exc:
                self._invalidated.set()
                raise NodeExecutionFailure("node_foreground_lease_invalidated") from exc

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self.pulse()
            except NodeExecutionFailure:
                return


class NodeTaskExecutor(Protocol):
    """Private server adapter for a leased remote or local-node operation."""

    def execute(self, lease: NodeWorkLease) -> NodeExecutionResult: ...


@dataclass(frozen=True)
class NodeTaskRun:
    """Bounded outcome emitted after an existing derived-work transition."""

    job_id: str
    project_id: str
    outcome: str
    node_id: str | None
    failure_code: str | None = None


@dataclass(frozen=True)
class AcceleratorBudget:
    """Hard admission budget for non-generative background work."""

    enabled: bool
    max_concurrency: int
    max_queue_depth: int
    max_daily_tasks: int
    min_free_memory_mib: int

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise NodeGovernanceError("accelerator_enabled_invalid")
        for value, code in (
            (self.max_concurrency, "accelerator_max_concurrency_invalid"),
            (self.max_queue_depth, "accelerator_max_queue_depth_invalid"),
            (self.max_daily_tasks, "accelerator_max_daily_tasks_invalid"),
            (self.min_free_memory_mib, "accelerator_min_free_memory_invalid"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise NodeGovernanceError(code)


@dataclass(frozen=True)
class AcceleratorAdmission:
    accepted: bool
    reason: str


def accelerator_admission(
    budget: AcceleratorBudget,
    *,
    task_kind: str,
    active_tasks: int,
    queued_tasks: int,
    completed_today: int,
    free_memory_mib: int,
) -> AcceleratorAdmission:
    """Admit only bounded, non-generative background work to durable outbox."""

    if not isinstance(budget, AcceleratorBudget):
        raise NodeGovernanceError("accelerator_budget_invalid")
    if not is_accelerator_task_kind(task_kind):
        return AcceleratorAdmission(False, "accelerator_task_kind_forbidden")
    for value, code in (
        (active_tasks, "accelerator_active_tasks_invalid"),
        (queued_tasks, "accelerator_queued_tasks_invalid"),
        (completed_today, "accelerator_completed_today_invalid"),
        (free_memory_mib, "accelerator_free_memory_invalid"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise NodeGovernanceError(code)
    if not budget.enabled:
        return AcceleratorAdmission(False, "accelerator_disabled")
    if active_tasks >= budget.max_concurrency:
        return AcceleratorAdmission(False, "accelerator_concurrency_budget_exhausted")
    if queued_tasks >= budget.max_queue_depth:
        return AcceleratorAdmission(False, "accelerator_queue_budget_exhausted")
    if completed_today >= budget.max_daily_tasks:
        return AcceleratorAdmission(False, "accelerator_daily_budget_exhausted")
    if free_memory_mib < budget.min_free_memory_mib:
        return AcceleratorAdmission(False, "accelerator_memory_budget_exhausted")
    return AcceleratorAdmission(True, "accelerator_admitted")


def fallback_chain_for(operation: str) -> tuple[str, ...]:
    """Return the operation's safe, observable degradation chain."""

    return _operation_policy(operation).fallback_chain


def task_priority_for(operation: str, *, accelerator_max: bool = False) -> int:
    """Return a fixed foreground-first priority class."""

    if accelerator_max:
        if not is_accelerator_task_kind(operation):
            raise NodeGovernanceError("accelerator_task_kind_forbidden")
        return 10
    return _operation_policy(operation).priority


def open_server_node_governance(
    *, clock: Callable[[], datetime] | None = None
) -> NodeGovernanceStore:
    """Open only the already-migrated server canonical SQLite registry."""

    return NodeGovernanceStore(
        Path(get_db_path()).expanduser(), clock=clock, _server_store_token=_SERVER_STORE_TOKEN
    )


def open_server_node_registration_authority(
    *, clock: Callable[[], datetime] | None = None
) -> NodeRegistrationAuthority:
    """Open the receipt issuer inside the server runtime only."""

    return NodeRegistrationAuthority(
        clock=clock or (lambda: datetime.now(timezone.utc)), _token=_SERVER_STORE_TOKEN
    )


def _open_node_governance_for_test(
    canonical_db_path: str | Path,
    *,
    clock: Callable[[], datetime] | None = None,
) -> NodeGovernanceStore:
    """Test-only opener for an explicitly pre-migrated temporary SQLite file."""

    return NodeGovernanceStore(
        canonical_db_path, clock=clock, _server_store_token=_SERVER_STORE_TOKEN
    )


def _open_node_registration_authority_for_test(
    *, clock: Callable[[], datetime] | None = None
) -> NodeRegistrationAuthority:
    return NodeRegistrationAuthority(
        clock=clock or (lambda: datetime.now(timezone.utc)), _token=_SERVER_STORE_TOKEN
    )


class NodeGovernanceStore:
    """Deep registry module for verified nodes, health, reservations and evidence."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        busy_timeout_ms: int = 10_000,
        health_ttl_seconds: int = _DEFAULT_HEALTH_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        _server_store_token: object | None = None,
    ) -> None:
        if _server_store_token is not _SERVER_STORE_TOKEN:
            raise NodeGovernanceError("node_governance_factory_required")
        raw_path = str(db_path)
        if not raw_path or raw_path == ":memory:" or "\x00" in raw_path:
            raise NodeGovernanceError("node_governance_db_path_invalid")
        if not isinstance(busy_timeout_ms, int) or not 1 <= busy_timeout_ms <= 120_000:
            raise NodeGovernanceError("node_governance_busy_timeout_invalid")
        if not isinstance(health_ttl_seconds, int) or not 1 <= health_ttl_seconds <= 86_400:
            raise NodeGovernanceError("node_governance_health_ttl_invalid")
        self._db_path = Path(raw_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._health_ttl_seconds = health_ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._verify_schema()

    def register(self, verified: VerifiedNodeRegistration) -> NodeSnapshot:
        """Persist only a server-issued, source-bound registration receipt."""

        if not isinstance(verified, VerifiedNodeRegistration):
            raise NodeGovernanceError("node_verification_receipt_invalid")
        registration = verified.registration
        receipt = verified.receipt
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM inference_nodes WHERE node_id = ?", (registration.node_id,)
            ).fetchone()
            expected_json = _json_text(registration.expected_identity.to_dict())
            capabilities_json = _json_text(list(registration.capabilities))
            now = _utc_text(self._now())
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO inference_nodes (
                        node_id, node_kind, transport_id, transport_evidence,
                        expected_identity_json, declared_capabilities_json,
                        max_concurrency, state, queue_depth, reported_available_slots,
                        registration_source, registration_reference, verification_receipt,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'registered', 0, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        registration.node_id,
                        registration.node_kind,
                        registration.transport_id,
                        registration.transport_evidence,
                        expected_json,
                        capabilities_json,
                        registration.max_concurrency,
                        receipt.source,
                        receipt.reference,
                        receipt.evidence_digest,
                        now,
                        now,
                    ),
                )
            else:
                if existing["node_kind"] != registration.node_kind:
                    raise NodeGovernanceError("node_kind_replacement_forbidden")
                if existing["expected_identity_json"] != expected_json:
                    raise NodeGovernanceError("node_identity_replacement_forbidden")
                if existing["declared_capabilities_json"] != capabilities_json:
                    raise NodeGovernanceError("node_capability_replacement_forbidden")
                connection.execute(
                    """
                    UPDATE inference_nodes
                    SET transport_id = ?, transport_evidence = ?, max_concurrency = ?,
                        registration_source = ?, registration_reference = ?,
                        verification_receipt = ?, updated_at = ?
                    WHERE node_id = ?
                    """,
                    (
                        registration.transport_id,
                        registration.transport_evidence,
                        registration.max_concurrency,
                        receipt.source,
                        receipt.reference,
                        receipt.evidence_digest,
                        now,
                        registration.node_id,
                    ),
                )
            snapshot = self._row_to_node(
                connection,
                connection.execute(
                    "SELECT * FROM inference_nodes WHERE node_id = ?", (registration.node_id,)
                ).fetchone(),
                now=_parse_utc(now),
            )
            connection.commit()
            return snapshot
        except NodeGovernanceError:
            _rollback(connection)
            raise
        except sqlite3.Error:
            _rollback(connection)
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def observe_health(self, evidence: NodeHealthEvidence) -> NodeSnapshot:
        """Accept authenticated health evidence or quarantine identity drift."""

        if not isinstance(evidence, NodeHealthEvidence):
            raise NodeGovernanceError("node_health_evidence_invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM inference_nodes WHERE node_id = ?", (evidence.node_id,)
            ).fetchone()
            if row is None:
                raise NodeGovernanceError("node_not_registered")
            expected = _identity_from_json(row["expected_identity_json"])
            capabilities = _json_string_tuple(row["declared_capabilities_json"])
            if not set(evidence.capabilities).issubset(capabilities):
                raise NodeGovernanceError("node_health_capability_unregistered")
            now = _utc_text(self._now())
            observed_json = _json_text(evidence.observed_identity.to_dict())
            capabilities_json = _json_text(list(evidence.capabilities))
            if expected != evidence.observed_identity:
                connection.execute(
                    """
                    UPDATE inference_nodes
                    SET state = 'quarantined', quarantine_reason = 'node_identity_drift',
                        observed_identity_json = ?, observed_capabilities_json = ?,
                        queue_depth = ?, reported_available_slots = ?, last_health_at = ?, updated_at = ?
                    WHERE node_id = ?
                    """,
                    (
                        observed_json,
                        capabilities_json,
                        evidence.queue_depth,
                        min(evidence.available_slots, int(row["max_concurrency"])),
                        now,
                        now,
                        evidence.node_id,
                    ),
                )
                self._record_audit(connection, evidence.node_id, "node_identity_drift", now)
            else:
                connection.execute(
                    """
                    UPDATE inference_nodes
                    SET state = 'active', quarantine_reason = NULL,
                        observed_identity_json = ?, observed_capabilities_json = ?,
                        queue_depth = ?, reported_available_slots = ?, last_health_at = ?, updated_at = ?
                    WHERE node_id = ?
                    """,
                    (
                        observed_json,
                        capabilities_json,
                        evidence.queue_depth,
                        min(evidence.available_slots, int(row["max_concurrency"])),
                        now,
                        now,
                        evidence.node_id,
                    ),
                )
                if row["state"] == "quarantined":
                    self._record_audit(connection, evidence.node_id, "node_identity_recovered", now)
            snapshot = self._row_to_node(
                connection,
                connection.execute(
                    "SELECT * FROM inference_nodes WHERE node_id = ?", (evidence.node_id,)
                ).fetchone(),
                now=_parse_utc(now),
            )
            connection.commit()
            return snapshot
        except NodeGovernanceError:
            _rollback(connection)
            raise
        except sqlite3.Error:
            _rollback(connection)
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def record_identity_revalidation(
        self,
        *,
        node_id: str,
        config_revision: str,
        required_identity: str,
        profile_digest: str,
        verification_receipt: NodeVerificationReceipt,
    ) -> NodeIdentityRevalidationReceipt:
        """Persist a profile receipt after controlled-revision and fresh-health proof."""

        _identifier(node_id, "node_id_invalid")
        _controlled_revision(config_revision, "node_identity_receipt_revision_invalid")
        _digest(required_identity, "node_identity_receipt_required_identity_invalid")
        _digest(profile_digest, "node_identity_receipt_profile_digest_invalid")
        if (
            not isinstance(verification_receipt, NodeVerificationReceipt)
            or verification_receipt.source != "controlled-revision"
            or verification_receipt.reference != config_revision
        ):
            raise NodeGovernanceError("node_identity_receipt_activation_evidence_invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, observed_identity_json, expected_identity_json, "
                "last_health_at, registration_source, registration_reference, "
                "verification_receipt "
                "FROM inference_nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            if row is None:
                raise NodeGovernanceError("node_not_registered")
            now = self._now()
            last_health_at = row["last_health_at"]
            if (
                row["state"] != "active"
                or not row["observed_identity_json"]
                or not last_health_at
                or _parse_utc(last_health_at)
                + timedelta(seconds=self._health_ttl_seconds)
                < now
            ):
                raise NodeGovernanceError("node_identity_revalidation_required")
            if (
                row["registration_source"] != verification_receipt.source
                or row["registration_reference"] != verification_receipt.reference
                or not hmac.compare_digest(
                    str(row["verification_receipt"]),
                    verification_receipt.evidence_digest,
                )
            ):
                raise NodeGovernanceError(
                    "node_identity_receipt_activation_evidence_invalid"
                )
            observed = _identity_from_json(row["observed_identity_json"])
            expected = _identity_from_json(row["expected_identity_json"])
            identity_matches = (
                _identity_matches(_operation_policy("embedding"), observed, required_identity)
                or _identity_matches(_operation_policy("rerank"), observed, required_identity)
                or (
                    observed.structured_json_key is not None
                    and observed.structured_json_key == required_identity
                )
            )
            if observed != expected or not identity_matches:
                raise NodeGovernanceError("node_identity_revalidation_mismatch")
            issued_at = _utc_text(now)
            receipt_id = "identity-receipt:" + uuid.uuid4().hex
            connection.execute(
                """
                INSERT OR REPLACE INTO inference_node_identity_receipts (
                    receipt_id, node_id, config_revision, required_identity,
                    observed_identity, profile_digest, issued_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    node_id,
                    config_revision,
                    required_identity,
                    _identity_digest(observed.to_dict()),
                    profile_digest,
                    issued_at,
                ),
            )
            connection.commit()
            return NodeIdentityRevalidationReceipt(
                receipt_id=receipt_id,
                node_id=node_id,
                config_revision=config_revision,
                required_identity=required_identity,
                observed_identity=_identity_digest(observed.to_dict()),
                profile_digest=profile_digest,
                issued_at=issued_at,
            )
        except NodeGovernanceError:
            _rollback(connection)
            raise
        except sqlite3.Error:
            _rollback(connection)
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def quarantine(self, node_id: str, reason: str) -> NodeSnapshot:
        _identifier(node_id, "node_id_invalid")
        if _FAILURE_CODE_RE.fullmatch(reason) is None:
            raise NodeGovernanceError("node_quarantine_reason_invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM inference_nodes WHERE node_id = ?", (node_id,)
                ).fetchone()
                is None
            ):
                raise NodeGovernanceError("node_not_registered")
            now = _utc_text(self._now())
            connection.execute(
                "UPDATE inference_nodes SET state = 'quarantined', quarantine_reason = ?, updated_at = ? "
                "WHERE node_id = ?",
                (reason, now, node_id),
            )
            event = (
                "node_execution_identity_drift"
                if reason == "node_identity_drift"
                else "node_explicit_quarantine"
            )
            self._record_audit(connection, node_id, event, now)
            snapshot = self._row_to_node(
                connection,
                connection.execute(
                    "SELECT * FROM inference_nodes WHERE node_id = ?", (node_id,)
                ).fetchone(),
                now=_parse_utc(now),
            )
            connection.commit()
            return snapshot
        except NodeGovernanceError:
            _rollback(connection)
            raise
        except sqlite3.Error:
            _rollback(connection)
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def require_node(self, node_id: str) -> NodeSnapshot:
        _identifier(node_id, "node_id_invalid")
        connection = self._connect()
        try:
            now = self._now()
            return self._row_to_node(
                connection,
                connection.execute(
                    "SELECT * FROM inference_nodes WHERE node_id = ?", (node_id,)
                ).fetchone(),
                now=now,
            )
        except sqlite3.Error:
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def reserve(
        self,
        *,
        job_id: str,
        project_id: str,
        fencing_generation: int,
        resolved: ResolvedNodeTask,
        lease_expires_at: str,
    ) -> NodeSelection | None:
        """Atomically choose and reserve a node for an existing durable lease."""

        _identifier(job_id, "node_task_id_invalid")
        _identifier(project_id, "node_task_project_id_invalid")
        if project_id != resolved.project_id:
            raise NodeGovernanceError("node_task_project_mismatch")
        if not isinstance(fencing_generation, int) or fencing_generation < 1:
            raise NodeGovernanceError("node_task_fencing_generation_invalid")
        expiry = _parse_utc(lease_expires_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            now_text = _utc_text(now)
            connection.execute(
                "DELETE FROM inference_node_reservations WHERE lease_expires_at <= ?", (now_text,)
            )
            existing = connection.execute(
                "SELECT node_id FROM inference_node_reservations "
                "WHERE job_id = ? AND fencing_generation = ?",
                (job_id, fencing_generation),
            ).fetchone()
            if existing is not None:
                node = self._row_to_node(
                    connection,
                    connection.execute(
                        "SELECT * FROM inference_nodes WHERE node_id = ?", (existing["node_id"],)
                    ).fetchone(),
                    now=now,
                )
                return NodeSelection(node, "existing-reservation")
            rows = connection.execute("SELECT * FROM inference_nodes").fetchall()
            selection = self._select_node(
                connection,
                (self._row_to_node(connection, row, now=now) for row in rows),
                resolved,
                now,
            )
            if selection is None:
                connection.commit()
                return None
            connection.execute(
                """
                INSERT INTO inference_node_reservations (
                    job_id, fencing_generation, project_id, node_id, lease_expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    fencing_generation,
                    project_id,
                    selection.node.node_id,
                    _utc_lease_ceiling_text(expiry),
                    now_text,
                ),
            )
            connection.commit()
            return selection
        except NodeGovernanceError:
            _rollback(connection)
            raise
        except sqlite3.Error:
            _rollback(connection)
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def release_reservation(self, *, job_id: str, fencing_generation: int) -> None:
        _identifier(job_id, "node_task_id_invalid")
        if not isinstance(fencing_generation, int) or fencing_generation < 1:
            raise NodeGovernanceError("node_task_fencing_generation_invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM inference_node_reservations WHERE job_id = ? AND fencing_generation = ?",
                (job_id, fencing_generation),
            )
            connection.commit()
        except sqlite3.Error:
            _rollback(connection)
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def renew_reservation(
        self,
        *,
        job_id: str,
        fencing_generation: int,
        lease_expires_at: str,
    ) -> None:
        """Extend one live reservation without weakening its fencing CAS."""

        _identifier(job_id, "node_task_id_invalid")
        if not isinstance(fencing_generation, int) or fencing_generation < 1:
            raise NodeGovernanceError("node_task_fencing_generation_invalid")
        expiry = _parse_utc(lease_expires_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            now_text = _utc_text(now)
            expiry_text = _utc_lease_ceiling_text(expiry)
            if expiry <= now:
                raise NodeGovernanceError("node_reservation_renewal_conflict")
            updated = connection.execute(
                """
                UPDATE inference_node_reservations
                SET lease_expires_at = CASE
                    WHEN lease_expires_at < ? THEN ?
                    ELSE lease_expires_at
                END
                WHERE job_id = ? AND fencing_generation = ? AND lease_expires_at > ?
                """,
                (
                    expiry_text,
                    expiry_text,
                    job_id,
                    fencing_generation,
                    now_text,
                ),
            ).rowcount
            if updated != 1:
                raise NodeGovernanceError("node_reservation_renewal_conflict")
            connection.commit()
        except NodeGovernanceError:
            _rollback(connection)
            raise
        except sqlite3.Error:
            _rollback(connection)
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def reconcile_reservations(self) -> int:
        """Release expired reservations after durable-work lease reconciliation."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM inference_node_reservations WHERE lease_expires_at <= ?",
                (_utc_text(self._now()),),
            ).rowcount
            connection.commit()
            return int(deleted)
        except sqlite3.Error:
            _rollback(connection)
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def record_success_latency(
        self,
        node_id: str,
        operation: str,
        required_identity: str,
        *,
        latency_ms: float | int,
    ) -> None:
        _identifier(node_id, "node_id_invalid")
        policy = _operation_policy(operation)
        _digest(required_identity, "node_task_required_identity_invalid")
        latency = _latency(latency_ms)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            node = connection.execute(
                "SELECT * FROM inference_nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if node is None:
                raise NodeGovernanceError("node_not_registered")
            if not _identity_matches(
                policy, _identity_from_json(node["expected_identity_json"]), required_identity
            ):
                raise NodeGovernanceError("node_latency_identity_mismatch")
            connection.execute(
                """
                INSERT INTO inference_node_latency_samples (
                    node_id, operation, required_identity, latency_ms, succeeded_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (node_id, operation, required_identity, latency, _utc_text(self._now())),
            )
            connection.commit()
        except NodeGovernanceError:
            _rollback(connection)
            raise
        except sqlite3.Error:
            _rollback(connection)
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def status(self) -> dict[str, object]:
        """Return a bounded, read-only registry projection for diagnostics."""

        connection = self._connect()
        try:
            now = self._now()
            node_counts = dict.fromkeys(("registered", "active", "quarantined"), 0)
            for row in connection.execute(
                "SELECT state, COUNT(*) AS count FROM inference_nodes GROUP BY state"
            ):
                node_counts[str(row["state"])] = int(row["count"])
            audit = connection.execute(
                "SELECT COUNT(*) AS count FROM inference_node_audit_events"
            ).fetchone()
            reservations = connection.execute(
                "SELECT COUNT(*) AS count FROM inference_node_reservations WHERE lease_expires_at > ?",
                (_utc_text(now),),
            ).fetchone()
            return {
                "schema": "plastic-promise/node-governance-status/v2",
                "state": "ready",
                "nodes": node_counts,
                "active_reservations": int(reservations["count"]),
                "audit_event_count": int(audit["count"]),
            }
        except sqlite3.Error:
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def dashboard_projection(self) -> dict[str, object]:
        """Return a safe, bounded node projection for authenticated operators.

        This is the single seam between the server-owned registry and the
        Dashboard.  It deliberately exposes scheduling-relevant observations
        while keeping private transports, receipts, request payloads and raw
        node evidence inside the registry implementation.
        """

        connection = self._connect()
        try:
            now = self._now()
            node_counts = dict.fromkeys(("registered", "active", "quarantined"), 0)
            for row in connection.execute(
                "SELECT state, COUNT(*) AS count FROM inference_nodes GROUP BY state"
            ):
                node_counts[str(row["state"])] = int(row["count"])
            reservations = connection.execute(
                "SELECT COUNT(*) AS count FROM inference_node_reservations WHERE lease_expires_at > ?",
                (_utc_text(now),),
            ).fetchone()
            audit = connection.execute(
                "SELECT COUNT(*) AS count FROM inference_node_audit_events"
            ).fetchone()
            nodes = [
                self._dashboard_node_projection(
                    connection,
                    self._row_to_node(connection, row, now=now),
                    now=now,
                )
                for row in connection.execute("SELECT * FROM inference_nodes ORDER BY node_id")
            ]
            return {
                "schema": "plastic-promise/node-governance-dashboard/v1",
                "state": "ready",
                "summary": {
                    "nodes": node_counts,
                    "active_reservations": int(reservations["count"]),
                    "audit_event_count": int(audit["count"]),
                },
                "nodes": nodes,
                "recent_routes": self._dashboard_recent_routes(connection),
                "derived_work": self._dashboard_derived_work_summary(connection),
                "accelerator_audit": self._dashboard_accelerator_audit(connection, now=now),
            }
        except (sqlite3.Error, NodeGovernanceError):
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def _select_node(
        self,
        connection: sqlite3.Connection,
        nodes: Iterable[NodeSnapshot],
        resolved: ResolvedNodeTask,
        now: datetime,
    ) -> NodeSelection | None:
        eligible = [
            node
            for node in nodes
            if self._eligible(connection, node, resolved, now)
        ]
        if resolved.allowed_node_ids:
            eligible = [node for node in eligible if node.node_id in resolved.allowed_node_ids]
        if resolved.scheduling_policy == "pinned-node":
            for node in eligible:
                if node.node_id == resolved.pinned_node_id:
                    return NodeSelection(node, "pinned-node")
            return None
        if resolved.scheduling_policy == "fastest-estimated":
            samples = [
                (
                    node,
                    self._latency_estimate(
                        connection, node, resolved.operation, resolved.required_identity
                    ),
                )
                for node in eligible
            ]
            complete = [(node, estimate) for node, estimate in samples if estimate is not None]
            if complete:
                node, _estimate = min(
                    complete, key=lambda item: (cast("float", item[1]), item[0].node_id)
                )
                return NodeSelection(node, "fastest-estimated")
            return self._first_registered_compute_node(eligible, "fastest-sample-insufficient")
        # Provider selection is a compute-node concern.  These legacy kind
        # labels are only scheduling hints for already-registered nodes; they
        # do not authorize a server-side provider call.  A ``cloud`` kind is
        # therefore still a compute node selected over private transport.
        kinds = (
            ("ollama", "remote-node", "cloud")
            if resolved.scheduling_policy == "ollama-first"
            else ("remote-node", "ollama", "cloud")
        )
        return self._first_by_kind(eligible, kinds, resolved.scheduling_policy)

    @staticmethod
    def _first_registered_compute_node(
        nodes: Iterable[NodeSnapshot], reason: str
    ) -> NodeSelection | None:
        matching = sorted(
            nodes,
            key=lambda node: (node.queue_depth, -node.available_slots, node.node_id),
        )
        if not matching:
            return None
        return NodeSelection(matching[0], reason)

    @staticmethod
    def _first_by_kind(
        nodes: Iterable[NodeSnapshot], kinds: tuple[str, ...], reason: str
    ) -> NodeSelection | None:
        for kind in kinds:
            matching = sorted(
                (node for node in nodes if node.node_kind == kind),
                key=lambda node: (node.queue_depth, -node.available_slots, node.node_id),
            )
            if matching:
                return NodeSelection(matching[0], reason)
        return None

    def _eligible(
        self,
        connection: sqlite3.Connection,
        node: NodeSnapshot,
        resolved: ResolvedNodeTask,
        now: datetime,
    ) -> bool:
        if node.state != "active" or node.last_health_at is None or node.available_slots <= 0:
            return False
        if _parse_utc(node.last_health_at) + timedelta(seconds=self._health_ttl_seconds) < now:
            return False
        policy = _operation_policy(resolved.operation)
        if (
            policy.capability not in node.declared_capabilities
            or policy.capability not in node.observed_capabilities
        ):
            return False
        # The active control revision owns the provider-class decision.  A
        # local/cloud switch therefore takes effect on the next scheduling
        # decision without restarting the canonical backend.  Hybrid admits
        # every registered compute-node class and lets the scheduling policy
        # choose among them; the server still never invokes a provider itself.
        provider_class = node.expected_identity.provider_class
        if resolved.inference_mode == "local" and provider_class not in {"local", "hybrid"}:
            return False
        if resolved.inference_mode == "cloud" and provider_class not in {"cloud", "hybrid"}:
            return False
        if resolved.inference_mode not in {"local", "cloud", "hybrid"}:
            return False
        if not _identity_matches(policy, node.expected_identity, resolved.required_identity):
            return False
        return resolved.profile_digest is None or self._has_profile_receipt(
            connection,
            node,
            resolved,
        )

    def _has_profile_receipt(
        self,
        connection: sqlite3.Connection,
        node: NodeSnapshot,
        resolved: ResolvedNodeTask,
    ) -> bool:
        """Consume an explicit profile receipt; scheduling never manufactures evidence."""

        observed = node.observed_identity
        if observed is None or observed != node.expected_identity:
            return False
        policy = _operation_policy(resolved.operation)
        if not _identity_matches(policy, observed, resolved.required_identity):
            return False
        observed_digest = _identity_digest(observed.to_dict())
        row = connection.execute(
            """
            SELECT 1
            FROM inference_node_identity_receipts
            WHERE node_id = ? AND config_revision = ?
              AND required_identity = ? AND profile_digest = ?
              AND observed_identity = ?
            LIMIT 1
            """,
            (
                node.node_id,
                resolved.config_revision,
                resolved.required_identity,
                resolved.profile_digest,
                observed_digest,
            ),
        ).fetchone()
        return row is not None

    def _latency_estimate(
        self,
        connection: sqlite3.Connection,
        node: NodeSnapshot,
        operation: str,
        required_identity: str,
    ) -> float | None:
        rows = connection.execute(
            """
            SELECT latency_ms FROM inference_node_latency_samples
            WHERE node_id = ? AND operation = ? AND required_identity = ?
            ORDER BY sample_id DESC LIMIT 100
            """,
            (node.node_id, operation, required_identity),
        ).fetchall()
        if len(rows) < _FASTEST_MIN_SUCCESS_SAMPLES:
            return None
        median = float(statistics.median(float(row["latency_ms"]) for row in rows))
        return median * (1.0 + (node.queue_depth / max(node.available_slots, 1)))

    def _row_to_node(
        self, connection: sqlite3.Connection, row: sqlite3.Row | None, *, now: datetime
    ) -> NodeSnapshot:
        if row is None:
            raise NodeGovernanceError("node_not_registered")
        reservations = connection.execute(
            "SELECT COUNT(*) AS count FROM inference_node_reservations "
            "WHERE node_id = ? AND lease_expires_at > ?",
            (row["node_id"], _utc_text(now)),
        ).fetchone()
        active = int(reservations["count"])
        observed = row["observed_identity_json"]
        return NodeSnapshot(
            node_id=str(row["node_id"]),
            node_kind=str(row["node_kind"]),
            transport_id=str(row["transport_id"]),
            transport_evidence=str(row["transport_evidence"]),
            expected_identity=_identity_from_json(row["expected_identity_json"]),
            observed_identity=None if observed is None else _identity_from_json(observed),
            declared_capabilities=_json_string_tuple(row["declared_capabilities_json"]),
            observed_capabilities=_json_string_tuple(row["observed_capabilities_json"] or "[]"),
            max_concurrency=int(row["max_concurrency"]),
            queue_depth=int(row["queue_depth"]),
            available_slots=min(
                max(0, int(row["reported_available_slots"]) - active),
                max(0, int(row["max_concurrency"]) - active),
            ),
            active_lease_count=active,
            registration_source=str(row["registration_source"]),
            registration_reference=str(row["registration_reference"]),
            state=str(row["state"]),
            quarantine_reason=cast("str | None", row["quarantine_reason"]),
            last_health_at=cast("str | None", row["last_health_at"]),
        )

    def _dashboard_node_projection(
        self,
        connection: sqlite3.Connection,
        node: NodeSnapshot,
        *,
        now: datetime,
    ) -> dict[str, object]:
        """Project one snapshot without leaking its private transport binding."""

        expected = node.expected_identity
        return {
            "node_id": node.node_id,
            "node_kind": node.node_kind,
            "provider_class": expected.provider_class,
            "state": node.state,
            "health": {
                "state": self._dashboard_health_state(node, now=now),
                "last_observed_at": node.last_health_at,
            },
            "capabilities": {
                "declared": list(node.declared_capabilities),
                "observed": list(node.observed_capabilities),
            },
            "embedding": {
                "model": expected.embedding_model,
                "revision": expected.embedding_revision,
                "dimension": expected.embedding_dimension,
                "normalization": expected.embedding_normalization,
            },
            "rerank": {
                "model": expected.rerank_model,
                "revision": expected.rerank_revision,
            },
            "capacity": {
                "queue_depth": node.queue_depth,
                "available_slots": node.available_slots,
                "active_leases": node.active_lease_count,
                "max_concurrency": node.max_concurrency,
            },
            "latency": {
                "embedding": self._dashboard_latency_summary(
                    connection, node.node_id, "embedding", expected.embedding_key
                ),
                "rerank": self._dashboard_latency_summary(
                    connection, node.node_id, "rerank", expected.rerank_key
                ),
            },
            "quarantine_reason": node.quarantine_reason,
        }

    def _dashboard_health_state(self, node: NodeSnapshot, *, now: datetime) -> str:
        if node.last_health_at is None:
            return "missing"
        if _parse_utc(node.last_health_at) + timedelta(seconds=self._health_ttl_seconds) < now:
            return "stale"
        return "fresh"

    @staticmethod
    def _dashboard_latency_summary(
        connection: sqlite3.Connection,
        node_id: str,
        operation: str,
        required_identity: str,
    ) -> dict[str, float | int | None]:
        rows = connection.execute(
            """
            SELECT latency_ms FROM inference_node_latency_samples
            WHERE node_id = ? AND operation = ? AND required_identity = ?
            ORDER BY sample_id DESC LIMIT 100
            """,
            (node_id, operation, required_identity),
        ).fetchall()
        samples = [float(row["latency_ms"]) for row in rows]
        return {
            "sample_count": len(samples),
            "median_ms": None if not samples else float(statistics.median(samples)),
        }

    @staticmethod
    def _dashboard_recent_routes(connection: sqlite3.Connection) -> list[dict[str, str | None]]:
        """Project only bounded node-routing result codes from derived work.

        Derived-work payloads and evidence can contain opaque references.  This
        adapter never returns them: it admits only the stable outcome, node,
        selection, degradation and failure-code fields produced by the governed
        coordinator.
        """

        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'derived_work_jobs'"
        ).fetchone()
        if table is None:
            return []
        rows = connection.execute(
            """
            SELECT result_json, result_bytes, failure_code, updated_at
            FROM derived_work_jobs
            WHERE job_kind = ? AND (result_bytes IS NULL OR result_bytes <= 8192)
            ORDER BY updated_at DESC, job_id DESC
            LIMIT 20
            """,
            (_DERIVED_JOB_KIND,),
        ).fetchall()
        result: list[dict[str, str | None]] = []
        for row in rows:
            completion: Mapping[str, object] = {}
            raw_completion = row["result_json"]
            if isinstance(raw_completion, str):
                try:
                    decoded = json.loads(raw_completion)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, Mapping):
                    completion = decoded
            node_id = _dashboard_code(completion.get("node_id"))
            outcome = _dashboard_code(completion.get("outcome"))
            selection = _dashboard_code(completion.get("selection_reason"))
            degradation = _dashboard_code(completion.get("degradation_reason"))
            failure = _dashboard_code(row["failure_code"])
            if not any((node_id, outcome, selection, degradation, failure)):
                continue
            occurred_at = row["updated_at"]
            if not isinstance(occurred_at, str):
                continue
            try:
                safe_occurred_at = _utc_text(_parse_utc(occurred_at))
            except NodeGovernanceError:
                continue
            result.append(
                {
                    "node_id": node_id,
                    "outcome": outcome,
                    "selection_reason": selection,
                    "degradation_reason": degradation,
                    "failure_code": failure,
                    "occurred_at": safe_occurred_at,
                }
            )
        return result

    @staticmethod
    def _dashboard_derived_work_summary(
        connection: sqlite3.Connection,
    ) -> dict[str, dict[str, int]]:
        """Return zero-filled queue status summaries without job payloads."""

        statuses = ("pending", "retry_wait", "leased", "completed", "dead", "cancelled")
        summary = {
            "node_inference": dict.fromkeys(statuses, 0),
            "accelerator_max": dict.fromkeys(statuses, 0),
        }
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'derived_work_jobs'"
        ).fetchone()
        if table is None:
            return summary
        rows = connection.execute(
            """
            SELECT job_kind, status, COUNT(*) AS count
            FROM derived_work_jobs
            WHERE job_kind IN (?, ?)
            GROUP BY job_kind, status
            """,
            (_DERIVED_JOB_KIND, _ACCELERATOR_DERIVED_JOB_KIND),
        ).fetchall()
        names = {
            _DERIVED_JOB_KIND: "node_inference",
            _ACCELERATOR_DERIVED_JOB_KIND: "accelerator_max",
        }
        for row in rows:
            name = names.get(str(row["job_kind"]))
            status = str(row["status"])
            if name is not None and status in summary[name]:
                summary[name][status] = int(row["count"])
        return summary

    @staticmethod
    def _dashboard_accelerator_audit(
        connection: sqlite3.Connection,
        *,
        now: datetime,
    ) -> dict[str, object]:
        """Project bounded accelerator admissions and outcomes for operators.

        ``derived_work_jobs`` and ``derived_work_attempts`` are already the
        durable ledger for accelerator-max admission, lease and outcome
        transitions.  Reusing those records avoids creating a second task or
        audit queue.  This projection deliberately never returns a project,
        subject, provider, payload, result, evidence, lease token, or raw
        timestamp outside the normal bounded dashboard timestamp format.
        """

        projection: dict[str, object] = {"daily_admissions": 0, "recent_events": []}
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('derived_work_jobs', 'derived_work_attempts', "
                "'derived_work_daily_admissions', "
                "'derived_work_accelerator_audit_events')"
            )
        }
        if "derived_work_jobs" not in tables:
            return projection

        if "derived_work_daily_admissions" in tables:
            row = connection.execute(
                "SELECT admitted_count FROM derived_work_daily_admissions "
                "WHERE job_kind = ? AND day_utc = ?",
                (_ACCELERATOR_DERIVED_JOB_KIND, now.date().isoformat()),
            ).fetchone()
            projection["daily_admissions"] = 0 if row is None else int(row["admitted_count"])

        events: list[dict[str, str | None]] = []
        job_rows = connection.execute(
            """
            SELECT payload_json, status, failure_code, created_at, updated_at
            FROM derived_work_jobs
            WHERE job_kind = ? AND payload_bytes <= 8192
            ORDER BY updated_at DESC, job_id DESC
            LIMIT 20
            """,
            (_ACCELERATOR_DERIVED_JOB_KIND,),
        ).fetchall()
        for row in job_rows:
            task_kind = _dashboard_accelerator_task_kind(row["payload_json"])
            status = _dashboard_code(row["status"])
            occurred_at = _dashboard_timestamp(row["updated_at"])
            if task_kind is None or status is None or occurred_at is None:
                continue
            decision = {
                "pending": "admitted",
                "retry_wait": "retry_wait",
                "leased": "claimed",
                "completed": "completed",
                "dead": "dead",
                "cancelled": "cancelled",
            }.get(status)
            if decision is None:
                continue
            events.append(
                {
                    "event": "job_lifecycle",
                    "task_kind": task_kind,
                    "decision": decision,
                    "reason": _dashboard_code(row["failure_code"]),
                    "occurred_at": occurred_at,
                }
            )

        if "derived_work_attempts" in tables:
            attempt_rows = connection.execute(
                """
                SELECT jobs.payload_json, attempts.disposition, attempts.failure_code,
                       attempts.claimed_at, attempts.finished_at
                FROM derived_work_attempts AS attempts
                JOIN derived_work_jobs AS jobs ON jobs.job_id = attempts.job_id
                WHERE jobs.job_kind = ? AND jobs.payload_bytes <= 8192
                ORDER BY COALESCE(attempts.finished_at, attempts.claimed_at) DESC,
                         attempts.attempt_id DESC
                LIMIT 20
                """,
                (_ACCELERATOR_DERIVED_JOB_KIND,),
            ).fetchall()
            for row in attempt_rows:
                task_kind = _dashboard_accelerator_task_kind(row["payload_json"])
                disposition = _dashboard_code(row["disposition"])
                occurred_at = _dashboard_timestamp(row["finished_at"] or row["claimed_at"])
                if task_kind is None or disposition is None or occurred_at is None:
                    continue
                events.append(
                    {
                        "event": "attempt",
                        "task_kind": task_kind,
                        "decision": disposition,
                        "reason": _dashboard_code(row["failure_code"]),
                        "occurred_at": occurred_at,
                    }
                )

        if "derived_work_accelerator_audit_events" in tables:
            decision_rows = connection.execute(
                """
                SELECT event_kind, task_kind, decision, reason_code, occurred_at
                FROM derived_work_accelerator_audit_events
                ORDER BY occurred_at DESC, event_id DESC
                LIMIT 20
                """
            ).fetchall()
            for row in decision_rows:
                event = _dashboard_code(row["event_kind"])
                task_kind = _dashboard_accelerator_audit_task_kind(row["task_kind"])
                decision = _dashboard_code(row["decision"])
                reason = _dashboard_code(row["reason_code"])
                occurred_at = _dashboard_timestamp(row["occurred_at"])
                if (
                    event is None
                    or task_kind is None
                    or decision is None
                    or reason is None
                    or occurred_at is None
                ):
                    continue
                events.append(
                    {
                        "event": event,
                        "task_kind": task_kind,
                        "decision": decision,
                        "reason": reason,
                        "occurred_at": occurred_at,
                    }
                )

        events.sort(
            key=lambda item: (
                str(item["occurred_at"]),
                str(item["event"]),
                str(item["task_kind"]),
            ),
            reverse=True,
        )
        projection["recent_events"] = events[:20]
        return projection

    @staticmethod
    def _record_audit(
        connection: sqlite3.Connection, node_id: str, event_name: str, now_text: str
    ) -> None:
        connection.execute(
            """
            INSERT INTO inference_node_audit_events (event_id, node_id, event_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (f"node-audit:{uuid.uuid4().hex}", node_id, event_name, now_text),
        )

    def _connect(self) -> sqlite3.Connection:
        if not self._db_path.is_file():
            raise NodeGovernanceError("node_governance_schema_missing")
        try:
            connection = sqlite3.connect(
                f"{self._db_path.resolve().as_uri()}?mode=rw",
                uri=True,
                timeout=self._busy_timeout_ms / 1000,
            )
        except sqlite3.Error:
            raise NodeGovernanceError("node_governance_schema_missing") from None
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _verify_schema(self) -> None:
        connection = self._connect()
        try:
            if not node_governance_schema_present(connection):
                raise NodeGovernanceError("node_governance_schema_missing")
        except sqlite3.Error:
            raise NodeGovernanceError("node_governance_store_unavailable") from None
        finally:
            connection.close()

    def _now(self) -> datetime:
        return _require_clock(self._clock)


class NodeInferenceWorkCoordinator:
    """Deep task module connecting canonical authority, outbox and registry.

    Callers learn two operations: ``enqueue`` an untrusted canonical reference,
    then run/reconcile it in a server worker.  Policy, pinning, identity and
    project ownership all come from ``NodeTaskAuthority`` rather than the
    caller.  The coordinator cannot write canonical memory; its only durable
    writes are derived-work transitions, node evidence and reservations.
    """

    def __init__(
        self,
        *,
        registry: NodeGovernanceStore,
        derived_work: DerivedWorkStore,
        authority: NodeTaskAuthority,
        retry_delay_seconds: int = 10,
    ) -> None:
        if not isinstance(registry, NodeGovernanceStore):
            raise NodeGovernanceError("node_registry_invalid")
        if not isinstance(derived_work, DerivedWorkStore):
            raise NodeGovernanceError("node_derived_work_store_invalid")
        if not callable(getattr(authority, "resolve", None)) or not callable(
            getattr(authority, "verify", None)
        ):
            raise NodeGovernanceError("node_task_authority_invalid")
        if not isinstance(retry_delay_seconds, int) or not 0 <= retry_delay_seconds <= 3600:
            raise NodeGovernanceError("node_task_retry_delay_invalid")
        self._registry = registry
        self._derived_work = derived_work
        self._authority = authority
        self._retry_delay_seconds = retry_delay_seconds

    def enqueue(self, request: NodeTaskRequest) -> DerivedWorkCreateResult:
        """Resolve a canonical reference and append one idempotent outbox job."""

        if not isinstance(request, NodeTaskRequest):
            raise NodeGovernanceError("node_task_request_invalid")
        resolved = self._resolve(request)
        payload = _resolved_payload(resolved)
        return self._derived_work.enqueue(
            project_id=resolved.project_id,
            visibility=resolved.visibility,
            config_revision=resolved.config_revision,
            job_kind=_DERIVED_JOB_KIND,
            provider_identity=resolved.required_identity,
            subject_id=resolved.input_reference,
            subject_hash=resolved.subject_hash,
            dedupe_key="node-inference:"
            + _identity_digest(
                {
                    "project_id": resolved.project_id,
                    "idempotency_key": request.idempotency_key,
                    "operation": resolved.operation,
                    "input_reference": resolved.input_reference,
                    "subject_hash": resolved.subject_hash,
                    "config_revision": resolved.config_revision,
                    "identity": resolved.required_identity,
                }
            ),
            payload=payload,
            priority=task_priority_for(resolved.operation),
            max_attempts=resolved.max_attempts,
        )

    def run_next(
        self,
        project_id: str,
        executor: NodeTaskExecutor,
        *,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> NodeTaskRun | None:
        """Run one project-scoped durable lease using a verified node route."""

        _identifier(project_id, "node_task_project_id_invalid")
        if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise NodeGovernanceError("node_task_lease_seconds_invalid")
        if not callable(getattr(executor, "execute", None)):
            raise NodeGovernanceError("node_task_executor_invalid")
        lease = self._derived_work.claim_next(
            project_id=project_id, job_kind=_DERIVED_JOB_KIND, lease_seconds=lease_seconds
        )
        if lease is None:
            return None
        return self._run_lease(lease, project_id, executor)

    def run_job(
        self,
        *,
        job_id: str,
        project_id: str,
        executor: NodeTaskExecutor,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> NodeTaskRun:
        """Run one known durable job without allowing another job to overtake it."""

        _identifier(job_id, "node_task_job_id_invalid")
        _identifier(project_id, "node_task_project_id_invalid")
        if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise NodeGovernanceError("node_task_lease_seconds_invalid")
        if not callable(getattr(executor, "execute", None)):
            raise NodeGovernanceError("node_task_executor_invalid")
        try:
            lease = self._derived_work.claim(
                job_id=job_id,
                project_id=project_id,
                lease_seconds=lease_seconds,
            )
        except Exception as exc:
            raise NodeGovernanceError("node_task_not_claimable") from exc
        return self._run_lease(lease, project_id, executor)

    def execute_foreground(
        self,
        *,
        resolved: ResolvedNodeTask,
        request_fingerprint: str,
        executor: NodeTaskExecutor,
        lease_seconds: int = 30,
    ) -> tuple[NodeExecutionResult | None, str | None, str, str]:
        """Execute one process-local request under the governed route.

        A secret-free derived-work marker records the outcome without storing
        live input or provider/model output.  A ``None`` result is an explicit
        no-node decision; callers apply their documented bounded cloud/original
        fallback afterwards.
        """

        if not isinstance(resolved, ResolvedNodeTask):
            raise NodeGovernanceError("node_task_resolved_invalid")
        if not isinstance(request_fingerprint, str) or not request_fingerprint:
            raise NodeGovernanceError("node_task_fingerprint_invalid")
        if not callable(getattr(executor, "execute", None)):
            raise NodeGovernanceError("node_task_executor_invalid")
        if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise NodeGovernanceError("node_task_lease_seconds_invalid")
        digest = hashlib.sha256(request_fingerprint.encode("utf-8")).hexdigest()
        marker_payload = {
            **_resolved_payload(resolved),
            "foreground": True,
            "request_fingerprint": "sha256:" + digest,
        }
        marker_contract_digest = hashlib.sha256(
            _json_text(marker_payload).encode("utf-8")
        ).hexdigest()
        marker_dedupe_key = f"foreground-marker:{marker_contract_digest}"
        marker_arguments = {
            "project_id": resolved.project_id,
            "visibility": resolved.visibility,
            "config_revision": resolved.config_revision,
            "job_kind": _FOREGROUND_MARKER_KIND,
            "provider_identity": resolved.required_identity,
            "subject_id": resolved.input_reference,
            "subject_hash": resolved.subject_hash,
            "payload": marker_payload,
            "priority": task_priority_for(resolved.operation),
            "max_attempts": resolved.max_attempts,
        }
        marker = self._derived_work.enqueue(
            **marker_arguments,
            dedupe_key=marker_dedupe_key,
        )
        if not marker.created and marker.job.status not in {"pending", "retry_wait"}:
            marker = self._derived_work.enqueue(
                **marker_arguments,
                dedupe_key=f"{marker_dedupe_key}:{uuid.uuid4().hex}",
            )
        marker_lease = self._derived_work.claim(
            job_id=marker.job.job_id,
            project_id=resolved.project_id,
            lease_seconds=lease_seconds,
        )
        try:
            selection = self._registry.reserve(
                job_id=marker_lease.job.job_id,
                project_id=resolved.project_id,
                fencing_generation=marker_lease.job.fencing_generation,
                resolved=resolved,
                lease_expires_at=str(marker_lease.job.lease_expires_at or ""),
            )
        except Exception:
            self._fail_foreground_marker(
                marker_lease,
                resolved,
                "node_reservation_failed",
            )
            raise
        if selection is None:
            if resolved.operation == "rerank":
                self._derived_work.complete(
                    job_id=marker_lease.job.job_id,
                    project_id=resolved.project_id,
                    lease_token=marker_lease.lease_token,
                    fencing_generation=marker_lease.job.fencing_generation,
                    result={
                        "schema": "node-inference-foreground-marker/v1",
                        "outcome": "original-order",
                        "operation": resolved.operation,
                        "degradation_reason": "governed_node_deferred",
                        "receipt_reference": marker_lease.job.job_id,
                    },
                )
            else:
                self._fail_foreground_marker(
                    marker_lease,
                    resolved,
                    "governed_node_deferred",
                )
            return (
                None,
                None,
                "governed_node_deferred",
                marker_lease.job.job_id,
            )
        def renew_marker_lease() -> None:
            renewed = self._derived_work.renew_lease(
                job_id=marker_lease.job.job_id,
                project_id=resolved.project_id,
                lease_token=marker_lease.lease_token,
                fencing_generation=marker_lease.job.fencing_generation,
                lease_seconds=lease_seconds,
            )
            if renewed.lease_expires_at is None:
                raise NodeGovernanceError("node_foreground_lease_invalidated")
            self._registry.renew_reservation(
                job_id=marker_lease.job.job_id,
                fencing_generation=marker_lease.job.fencing_generation,
                lease_expires_at=renewed.lease_expires_at,
            )

        heartbeat = _ForegroundLeaseHeartbeat(
            renew_marker_lease,
            lease_seconds=lease_seconds,
        )
        lease = NodeWorkLease(
            derived_lease=marker_lease,
            resolved=resolved,
            node_id=selection.node.node_id,
            transport_id=selection.node.transport_id,
            selection_reason=selection.reason,
            _heartbeat=heartbeat.pulse,
        )
        try:
            heartbeat.start()
            result = executor.execute(lease)
            heartbeat.stop()
            heartbeat.pulse()
            if not isinstance(result, NodeExecutionResult):
                raise NodeExecutionFailure("node_execution_result_invalid")
            if resolved.operation in {"embedding", "rerank"}:
                self._registry.record_success_latency(
                    selection.node.node_id,
                    resolved.operation,
                    resolved.required_identity,
                    latency_ms=result.latency_ms,
                )
            result_digest = "sha256:" + hashlib.sha256(
                _json_text(dict(result.result)).encode("utf-8")
            ).hexdigest()
            self._derived_work.complete(
                job_id=marker_lease.job.job_id,
                project_id=resolved.project_id,
                lease_token=marker_lease.lease_token,
                fencing_generation=marker_lease.job.fencing_generation,
                result={
                    "schema": "node-inference-foreground-marker/v1",
                    "outcome": "completed",
                    "operation": resolved.operation,
                    "node_id": selection.node.node_id,
                    "selection_reason": selection.reason,
                    "provider_identity": resolved.required_identity,
                    "result_digest": result_digest,
                    "receipt_reference": marker_lease.job.job_id,
                },
            )
            return (
                result,
                selection.node.node_id,
                selection.reason,
                marker_lease.job.job_id,
            )
        except NodeExecutionFailure as exc:
            heartbeat.stop()
            if exc.code == "node_foreground_lease_invalidated" or heartbeat.invalidated:
                raise NodeExecutionFailure("node_foreground_lease_invalidated") from exc
            heartbeat.pulse()
            if exc.code in _OVERLOAD_DEFERRED_FAILURE_CODES:
                if resolved.operation == "rerank":
                    self._derived_work.complete(
                        job_id=marker_lease.job.job_id,
                        project_id=resolved.project_id,
                        lease_token=marker_lease.lease_token,
                        fencing_generation=marker_lease.job.fencing_generation,
                        result={
                            "schema": "node-inference-foreground-marker/v1",
                            "outcome": "original-order",
                            "operation": resolved.operation,
                            "degradation_reason": exc.code,
                            "receipt_reference": marker_lease.job.job_id,
                        },
                    )
                else:
                    self._fail_foreground_marker(marker_lease, resolved, exc.code)
                return (
                    None,
                    None,
                    exc.code,
                    marker_lease.job.job_id,
                )
            self._fail_foreground_marker(marker_lease, resolved, exc.code)
            self._quarantine_execution_identity_drift(selection.node.node_id, exc.code)
            self._quarantine_execution_transport_failure(selection.node.node_id, exc.code)
            raise
        except Exception as exc:
            heartbeat.stop()
            if heartbeat.invalidated:
                raise NodeExecutionFailure("node_foreground_lease_invalidated") from exc
            try:
                heartbeat.pulse()
            except NodeExecutionFailure as lease_error:
                raise lease_error from exc
            self._fail_foreground_marker(marker_lease, resolved, "node_execution_failed")
            self._quarantine_execution_transport_failure(
                selection.node.node_id, "node_execution_failed"
            )
            raise
        finally:
            heartbeat.stop()
            self._registry.release_reservation(
                job_id=marker_lease.job.job_id,
                fencing_generation=marker_lease.job.fencing_generation,
            )

    def _run_lease(
        self,
        lease: DerivedWorkLease,
        project_id: str,
        executor: NodeTaskExecutor,
    ) -> NodeTaskRun:
        """Execute an already claimed lease with authority and reservation checks."""

        resolved = _resolved_from_payload(lease.job)
        if resolved.project_id != project_id:
            return self._fail_untrusted_lease(lease, "node_task_project_mismatch")
        try:
            lease_verifier = getattr(self._authority, "verify_lease", None)
            if callable(lease_verifier):
                lease_verifier(resolved)
            else:
                self._authority.verify(resolved)
        except NodeGovernanceError as exc:
            return self._fail_untrusted_lease(lease, exc.code)
        except Exception:
            return self._fail_untrusted_lease(lease, "node_task_reference_unavailable")

        selection = self._registry.reserve(
            job_id=lease.job.job_id,
            project_id=project_id,
            fencing_generation=lease.job.fencing_generation,
            resolved=resolved,
            lease_expires_at=str(lease.job.lease_expires_at or ""),
        )
        if selection is None:
            return self._complete_fallback(lease, resolved, executor)
        work_lease = NodeWorkLease(
            derived_lease=lease,
            resolved=resolved,
            node_id=selection.node.node_id,
            transport_id=selection.node.transport_id,
            selection_reason=selection.reason,
        )
        try:
            result = executor.execute(work_lease)
            if not isinstance(result, NodeExecutionResult):
                raise NodeExecutionFailure("node_execution_result_invalid")
            self._registry.record_success_latency(
                selection.node.node_id,
                resolved.operation,
                resolved.required_identity,
                latency_ms=result.latency_ms,
            )
            completion = {
                "outcome": "completed",
                "node_id": selection.node.node_id,
                "selection_reason": selection.reason,
                "latency_ms": _latency(result.latency_ms),
                "evidence": _safe_evidence(result.evidence),
            }
            if result.result:
                completion["result"] = _safe_result(result.result)
            self._derived_work.complete(
                job_id=lease.job.job_id,
                project_id=project_id,
                lease_token=lease.lease_token,
                fencing_generation=lease.job.fencing_generation,
                result=completion,
            )
            return NodeTaskRun(lease.job.job_id, project_id, "completed", selection.node.node_id)
        except NodeExecutionFailure as exc:
            self._quarantine_execution_identity_drift(selection.node.node_id, exc.code)
            self._quarantine_execution_transport_failure(selection.node.node_id, exc.code)
            return self._fail_execution(lease, selection.node.node_id, exc.code)
        except Exception:
            self._quarantine_execution_transport_failure(
                selection.node.node_id, "node_execution_failed"
            )
            return self._fail_execution(lease, selection.node.node_id, "node_execution_failed")
        finally:
            self._registry.release_reservation(
                job_id=lease.job.job_id, fencing_generation=lease.job.fencing_generation
            )

    def _quarantine_execution_identity_drift(self, node_id: str, failure_code: str) -> None:
        """Persist drift isolation before the next scheduler selection."""

        if failure_code not in _IDENTITY_DRIFT_FAILURE_CODES:
            return
        try:
            self._registry.quarantine(node_id, "node_identity_drift")
        except NodeGovernanceError:
            # Preserve the execution failure; a later health probe will retry
            # isolation if the registry was temporarily unavailable.
            return

    def _quarantine_execution_transport_failure(self, node_id: str, failure_code: str) -> None:
        """Isolate an untrustworthy execution path until a fresh health probe."""

        if not _is_quarantinable_execution_failure(failure_code):
            return
        try:
            self._registry.quarantine(node_id, "node_transport_unavailable")
        except NodeGovernanceError:
            return

    def _fail_foreground_marker(
        self,
        marker_lease: DerivedWorkLease,
        resolved: ResolvedNodeTask,
        failure_code: str,
    ) -> None:
        self._derived_work.fail(
            job_id=marker_lease.job.job_id,
            project_id=marker_lease.job.project_id,
            lease_token=marker_lease.lease_token,
            fencing_generation=marker_lease.job.fencing_generation,
            failure_code=failure_code,
            retryable=(
                resolved.operation in {"embedding", "structured-json"}
                and failure_code not in _LOCAL_REQUEST_FAILURE_CODES
            ),
        )

    def reconcile(self, *, project_id: str | None = None) -> dict[str, int]:
        """Reconcile expired derived leases and their corresponding reservations."""

        if project_id is not None:
            _identifier(project_id, "node_task_project_id_invalid")
        recovered = self._derived_work.recover_expired(project_id=project_id)
        released = self._registry.reconcile_reservations()
        return {"derived_work_recovered": recovered, "reservations_released": released}

    def status(self, *, project_id: str | None = None) -> dict[str, object]:
        """Merge registry status with existing derived-work outbox metrics."""

        registry = self._registry.status()
        metrics = self._derived_work.stats(project_id=project_id, job_kind=_DERIVED_JOB_KIND)
        return {**registry, "derived_work": metrics}

    def _resolve(self, request: NodeTaskRequest) -> ResolvedNodeTask:
        try:
            resolved = self._authority.resolve(request)
        except NodeGovernanceError:
            raise
        except Exception as exc:
            raise NodeGovernanceError("node_task_reference_unavailable") from exc
        if not isinstance(resolved, ResolvedNodeTask):
            raise NodeGovernanceError("node_task_resolution_invalid")
        if (
            resolved.project_id != request.project_id
            or resolved.operation != request.operation
            or resolved.input_reference != request.input_reference
        ):
            raise NodeGovernanceError("node_task_reference_ownership_invalid")
        self._authority.verify(resolved)
        return resolved

    def _complete_fallback(
        self,
        lease: DerivedWorkLease,
        resolved: ResolvedNodeTask,
        executor: NodeTaskExecutor,
    ) -> NodeTaskRun:
        if resolved.operation == "embedding":
            return self._fail_execution(lease, None, "embedding_identity_unavailable")
        if resolved.operation == "rerank":
            self._derived_work.complete(
                job_id=lease.job.job_id,
                project_id=lease.job.project_id,
                lease_token=lease.lease_token,
                fencing_generation=lease.job.fencing_generation,
                result={"outcome": "original-order", "degradation_reason": "rerank_unavailable"},
            )
            return NodeTaskRun(lease.job.job_id, lease.job.project_id, "original-order", None)
        if resolved.operation == "structured-json":
            return self._fail_execution(lease, None, "structured_json_identity_unavailable")
        return self._fail_execution(lease, None, "node_task_operation_invalid")

    def _fail_untrusted_lease(self, lease: DerivedWorkLease, code: str) -> NodeTaskRun:
        return self._fail_execution(lease, None, code)

    def _fail_execution(
        self, lease: DerivedWorkLease, node_id: str | None, failure_code: str
    ) -> NodeTaskRun:
        retry_delay = min(300, self._retry_delay_seconds * (2 ** min(lease.job.attempt_count, 5)))
        updated = self._derived_work.fail(
            job_id=lease.job.job_id,
            project_id=lease.job.project_id,
            lease_token=lease.lease_token,
            fencing_generation=lease.job.fencing_generation,
            failure_code=failure_code,
            retryable=True,
            retry_delay_seconds=retry_delay,
        )
        return NodeTaskRun(
            lease.job.job_id,
            lease.job.project_id,
            "retry-wait" if updated.status == "retry_wait" else "dead",
            node_id,
            failure_code,
        )


def _operation_policy(operation: object) -> OperationPolicy:
    if not isinstance(operation, str) or operation not in _OPERATION_POLICIES:
        raise NodeGovernanceError("node_task_operation_invalid")
    return _OPERATION_POLICIES[operation]


def _resolved_payload(resolved: ResolvedNodeTask) -> dict[str, object]:
    return {
        "schema": "node-inference-work/v1",
        "project_id": resolved.project_id,
        "operation": resolved.operation,
        "input_reference": resolved.input_reference,
        "subject_hash": resolved.subject_hash,
        "visibility": resolved.visibility,
        "config_revision": resolved.config_revision,
        "required_identity": resolved.required_identity,
        "scheduling_policy": resolved.scheduling_policy,
        "inference_mode": resolved.inference_mode,
        "pinned_node_id": resolved.pinned_node_id,
        "allowed_node_ids": list(resolved.allowed_node_ids),
        "profile_digest": resolved.profile_digest,
        "max_attempts": resolved.max_attempts,
    }


def _resolved_from_payload(job: object) -> ResolvedNodeTask:
    payload = getattr(job, "payload", None)
    if not isinstance(payload, Mapping) or payload.get("schema") != "node-inference-work/v1":
        raise NodeGovernanceError("node_task_payload_invalid")
    allowed = payload.get("allowed_node_ids", [])
    if not isinstance(allowed, list) or not all(isinstance(value, str) for value in allowed):
        raise NodeGovernanceError("node_task_payload_invalid")
    try:
        return ResolvedNodeTask(
            project_id=_string(payload.get("project_id"), "node_task_payload_invalid"),
            operation=_string(payload.get("operation"), "node_task_payload_invalid"),
            input_reference=_string(payload.get("input_reference"), "node_task_payload_invalid"),
            subject_hash=_string(payload.get("subject_hash"), "node_task_payload_invalid"),
            visibility=_string(payload.get("visibility"), "node_task_payload_invalid"),
            config_revision=_string(payload.get("config_revision"), "node_task_payload_invalid"),
            required_identity=_string(
                payload.get("required_identity"), "node_task_payload_invalid"
            ),
            scheduling_policy=_string(
                payload.get("scheduling_policy"), "node_task_payload_invalid"
            ),
            inference_mode=_string(
                payload.get("inference_mode", "hybrid"),
                "node_task_payload_invalid",
            ),
            pinned_node_id=cast("str | None", payload.get("pinned_node_id")),
            allowed_node_ids=tuple(allowed),
            profile_digest=cast("str | None", payload.get("profile_digest")),
            max_attempts=cast("int", payload.get("max_attempts")),
        )
    except (TypeError, ValueError) as exc:
        raise NodeGovernanceError("node_task_payload_invalid") from exc


def _identity_matches(
    policy: OperationPolicy, identity: NodeIdentityEvidence, required_identity: str
) -> bool:
    if policy.identity_kind == "embedding":
        return hmac.compare_digest(identity.embedding_key, required_identity)
    if policy.identity_kind == "rerank":
        return hmac.compare_digest(identity.rerank_key, required_identity)
    if policy.identity_kind == "structured-json":
        observed = identity.structured_json_key
        return observed is not None and hmac.compare_digest(observed, required_identity)
    return True


def _identity_digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise NodeGovernanceError(code)
    return value


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise NodeGovernanceError(code)
    return value


def _controlled_revision(value: object, code: str) -> str:
    if not isinstance(value, str) or _CONTROL_REVISION_RE.fullmatch(value) is None:
        raise NodeGovernanceError(code)
    return value


def _registration_reference(source: str, value: object) -> str:
    if source == "controlled-revision":
        return _controlled_revision(value, "node_registration_reference_invalid")
    return _identifier(value, "node_registration_reference_invalid")


def _safe_identity(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(character.isspace() for character in value)
    ):
        raise NodeGovernanceError(code)
    return value


def _dashboard_code(value: object) -> str | None:
    """Admit a stable, non-sensitive status code to the Dashboard projection."""

    if not isinstance(value, str) or _FAILURE_CODE_RE.fullmatch(value) is None:
        return None
    return value


def _dashboard_timestamp(value: object) -> str | None:
    """Normalize an already bounded SQLite timestamp for a public projection."""

    if not isinstance(value, str):
        return None
    try:
        return _utc_text(_parse_utc(value))
    except NodeGovernanceError:
        return None


def _dashboard_accelerator_task_kind(payload_json: object) -> str | None:
    """Extract only a known task-kind code from a durable job payload."""

    if not isinstance(payload_json, str):
        return None
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    task_kind = payload.get("task_kind")
    if not is_accelerator_task_kind(task_kind):
        return None
    return _dashboard_code(task_kind)


def _dashboard_accelerator_audit_task_kind(value: object) -> str | None:
    """Admit fixed accelerator task kinds plus the aggregate scheduler code."""

    if not isinstance(value, str):
        return None
    if value == "scheduler":
        return value
    if not is_accelerator_task_kind(value):
        return None
    return _dashboard_code(value)


def _pinned_revision(value: object, code: str) -> str:
    result = _safe_identity(value, code)
    if _REVISION_RE.fullmatch(result) is None:
        raise NodeGovernanceError(code)
    return result


def _capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise NodeGovernanceError("node_capabilities_invalid")
    if len(value) > 8 or any(item not in _OPERATION_POLICIES for item in value):
        raise NodeGovernanceError("node_capabilities_invalid")
    if len(set(value)) != len(value):
        raise NodeGovernanceError("node_capabilities_invalid")
    return tuple(sorted(value))


def _string(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise NodeGovernanceError(code)
    return value


def _positive_int(value: object, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise NodeGovernanceError(code)
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise NodeGovernanceError("node_identity_evidence_invalid")
    return value


def _json_text(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _identity_from_json(value: object) -> NodeIdentityEvidence:
    if not isinstance(value, str):
        raise NodeGovernanceError("node_identity_evidence_invalid")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        raise NodeGovernanceError("node_identity_evidence_invalid") from None
    return NodeIdentityEvidence.from_dict(_mapping(decoded))


def _json_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise NodeGovernanceError("node_capabilities_invalid")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        raise NodeGovernanceError("node_capabilities_invalid") from None
    if not isinstance(decoded, list):
        raise NodeGovernanceError("node_capabilities_invalid")
    return _capabilities(tuple(decoded)) if decoded else ()


def _latency(value: float | int) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise NodeGovernanceError("node_latency_invalid")
    result = float(value)
    if not math.isfinite(result) or not 0 < result <= 3_600_000:
        raise NodeGovernanceError("node_latency_invalid")
    return result


def _is_quarantinable_execution_failure(failure_code: str) -> bool:
    """Keep retry-safe caller validation separate from untrusted node failures."""

    if failure_code in _OVERLOAD_DEFERRED_FAILURE_CODES:
        return False
    if failure_code in _IDENTITY_DRIFT_FAILURE_CODES:
        return False
    if failure_code in _GENERIC_NODE_EXECUTION_FAILURE_CODES:
        return True
    return (
        failure_code.startswith("node_private_")
        and failure_code not in _LOCAL_REQUEST_FAILURE_CODES
    )


def _safe_evidence(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise NodeGovernanceError("node_execution_evidence_invalid")
    text = _json_text(dict(value))
    if len(text.encode("utf-8")) > 8_192:
        raise NodeGovernanceError("node_execution_evidence_too_large")
    return cast("dict[str, object]", json.loads(text))


def _safe_result(value: Mapping[str, object]) -> dict[str, object]:
    """Persist bounded derived output without allowing source text into evidence."""

    if not isinstance(value, Mapping):
        raise NodeGovernanceError("node_execution_result_invalid")
    text = _json_text(dict(value))
    if len(text.encode("utf-8")) > 1024 * 1024:
        raise NodeGovernanceError("node_execution_result_too_large")
    return cast("dict[str, object]", json.loads(text))


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise NodeGovernanceError("node_timestamp_invalid") from None
    if parsed.tzinfo is None:
        raise NodeGovernanceError("node_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_lease_ceiling_text(value: datetime) -> str:
    """Keep second-precision reservation time from expiring before its lease."""

    normalized = value.astimezone(timezone.utc)
    if normalized.microsecond:
        normalized = normalized.replace(microsecond=0) + timedelta(seconds=1)
    return _utc_text(normalized)


def _require_clock(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if not isinstance(now, datetime):
        raise NodeGovernanceError("node_governance_clock_invalid")
    if now.tzinfo is None:
        raise NodeGovernanceError("node_governance_clock_timezone_required")
    return now.astimezone(timezone.utc)


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


__all__ = [
    "AcceleratorAdmission",
    "AcceleratorBudget",
    "ControlledRevisionStore",
    "NodeExecutionFailure",
    "NodeExecutionResult",
    "NodeGovernanceError",
    "NodeGovernanceStore",
    "NodeHealthEvidence",
    "NodeIdentityEvidence",
    "NodeInferenceWorkCoordinator",
    "NodeRegistration",
    "NodeRegistrationAuthority",
    "NodeSelection",
    "NodeSnapshot",
    "NodeTaskAuthority",
    "NodeTaskExecutor",
    "NodeTaskRequest",
    "NodeTaskRun",
    "NodeVerificationReceipt",
    "NodeWorkLease",
    "OperationPolicy",
    "ResolvedNodeTask",
    "VerifiedNodeRegistration",
    "accelerator_admission",
    "fallback_chain_for",
    "open_server_node_governance",
    "open_server_node_registration_authority",
    "task_priority_for",
]
