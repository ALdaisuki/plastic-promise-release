"""Read-only Deployment Center contracts for the local-edge control surface.

``DeploymentCenter`` deliberately exposes only two operations: ``inspect`` and
``preview``.  ``inspect`` consumes only an opaque installation reference;
``preview`` additionally consumes a strict Deployment Manifest V2 candidate.
Both combine three injected read-only adapters:

* a resolver from an installation label to logical storage labels;
* a host inspector that reports bounded capacity facts; and
* a controller-state adapter that reports contract and model-identity evidence.

The module never accepts a filesystem path, shell command, SSH target, Docker
socket, credential, legacy deployment manifest, or mutable controller plan.
It also never activates a runtime.  PR 5 may turn an inspected plan into a
persisted, authorised deployment operation; the ``plan_hash`` returned here is
explicitly inspection-only evidence and is not an execution token.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from plastic_promise.endpoint_roles import compute_package_manifest

from .catalog import profile_by_id, stable_profile_ids
from .endpoint_contract import (
    ENDPOINT_CONTRACT_SCHEMA_VERSION,
    EndpointContractError,
    ResolvedEndpointDeploymentPlan,
    resolve_deployment_manifest_v2,
)

DEPLOYMENT_CENTER_SCHEMA_VERSION = "plastic-promise-deployment-center/v1"
DEPLOYMENT_CENTER_INSPECTION_SCHEMA_VERSION = "plastic-promise-deployment-inspection/v1"
DEPLOYMENT_CENTER_PREVIEW_SCHEMA_VERSION = "plastic-promise-deployment-preview/v1"

_SAFE_REFERENCE = re.compile(r"^[a-z][a-z0-9:_-]{1,127}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_:-]{1,127}$")
_SAFE_CONTRACT_SCHEMA = re.compile(r"^[a-z][a-z0-9-]{1,127}/v[1-9][0-9]*$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLATFORMS = frozenset({"macos", "linux", "windows", "wsl2"})
_COMPUTE_CONTRACTS = {
    capability.kind: capability.contract_version
    for capability in compute_package_manifest().capabilities
}
_UPDATE_CLASSES = frozenset(
    {
        "no-change",
        "live-apply",
        "rolling-restart",
        "shadow-rebuild-promotion",
        "backup-migration",
        "enrollment-required",
        "manual-review",
    }
)
_ENROLLMENT_STATUSES = frozenset({"ready", "required", "unavailable", "not_required"})
_MANIFEST_DIFF_AVAILABILITY = frozenset({"available", "unavailable"})


class DeploymentCenterError(ValueError):
    """A stable, non-sensitive failure from the Deployment Center interface."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("deployment_center_error_code_invalid")
        self.code = code
        super().__init__(code)

    def public_json(self) -> dict[str, str]:
        """Return the safe error envelope suitable for a local-edge caller."""

        return {"code": self.code}


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: Mapping[str, object]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


def _require_reference(value: object, code: str) -> str:
    if not isinstance(value, str) or _SAFE_REFERENCE.fullmatch(value) is None:
        raise DeploymentCenterError(code)
    return value


def _require_code(value: object, code: str) -> str:
    if not isinstance(value, str) or _SAFE_CODE.fullmatch(value) is None:
        raise DeploymentCenterError(code)
    return value


def _require_contract_schema(value: object, code: str) -> str:
    if not isinstance(value, str) or _SAFE_CONTRACT_SCHEMA.fullmatch(value) is None:
        raise DeploymentCenterError(code)
    return value


def _require_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DeploymentCenterError(code)
    return value


def _require_nonnegative_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DeploymentCenterError(code)
    return value


def _require_positive_int(value: object, code: str) -> int:
    integer = _require_nonnegative_int(value, code)
    if integer == 0:
        raise DeploymentCenterError(code)
    return integer


def _require_utc(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DeploymentCenterError(code)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class DeploymentPreviewRequest:
    """One strict V2 candidate plus an opaque installation label for preview.

    Changing profile is deliberately performed by submitting another complete
    V2 candidate manifest.  The advisory recommendation never rewrites the
    candidate, and this object has no path, transport, or execution field.
    """

    candidate_manifest: Mapping[str, object]
    installation_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_manifest, Mapping):
            raise DeploymentCenterError("deployment_center_candidate_manifest_required")
        _require_reference(
            self.installation_ref,
            "deployment_center_installation_reference_invalid",
        )


@dataclass(frozen=True)
class InstallationResolution:
    """Host-owned logical labels resolved from an opaque installation reference.

    ``canonical_state_ref`` is a storage label, not a path.  A production
    adapter may keep path/controller-plan details private while a test fake can
    map the same label to in-memory state.
    """

    installation_ref: str
    canonical_state_ref: str

    def __post_init__(self) -> None:
        _require_reference(
            self.installation_ref,
            "deployment_center_installation_reference_invalid",
        )
        _require_reference(
            self.canonical_state_ref,
            "deployment_center_canonical_state_reference_invalid",
        )


@dataclass(frozen=True)
class HostStorage:
    """One redacted capacity observation, keyed only by logical labels."""

    resource_ref: str
    volume_ref: str
    total_bytes: int
    free_bytes: int

    def __post_init__(self) -> None:
        _require_reference(self.resource_ref, "deployment_center_storage_reference_invalid")
        _require_reference(self.volume_ref, "deployment_center_volume_reference_invalid")
        total = _require_positive_int(self.total_bytes, "deployment_center_storage_total_invalid")
        free = _require_nonnegative_int(self.free_bytes, "deployment_center_storage_free_invalid")
        if free > total:
            raise DeploymentCenterError("deployment_center_storage_free_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_ref": self.resource_ref,
            "volume_ref": self.volume_ref,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
        }


@dataclass(frozen=True)
class HostInspection:
    """Bounded host evidence supplied by a read-only platform adapter."""

    platform: str
    observed_at: datetime
    freshness_seconds: int
    fresh: bool
    container_runtime_ready: bool
    accelerator_available: bool
    storage: tuple[HostStorage, ...]

    def __post_init__(self) -> None:
        if self.platform not in _PLATFORMS:
            raise DeploymentCenterError("deployment_center_platform_unsupported")
        _require_utc(self.observed_at, "deployment_center_host_observed_at_invalid")
        _require_positive_int(
            self.freshness_seconds,
            "deployment_center_host_freshness_invalid",
        )
        if not isinstance(self.fresh, bool):
            raise DeploymentCenterError("deployment_center_host_freshness_invalid")
        if not isinstance(self.container_runtime_ready, bool):
            raise DeploymentCenterError("deployment_center_container_runtime_invalid")
        if not isinstance(self.accelerator_available, bool):
            raise DeploymentCenterError("deployment_center_accelerator_invalid")
        if not self.storage:
            raise DeploymentCenterError("deployment_center_storage_required")
        references = [item.resource_ref for item in self.storage]
        if len(references) != len(set(references)):
            raise DeploymentCenterError("deployment_center_storage_reference_duplicate")

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "observed_at": _timestamp(self.observed_at),
            "freshness_seconds": self.freshness_seconds,
            "fresh": self.fresh,
            "container_runtime_ready": self.container_runtime_ready,
            "accelerator_available": self.accelerator_available,
            "storage": [item.to_dict() for item in self.storage],
        }


@dataclass(frozen=True)
class EndpointContractGate:
    """One server-state result for a candidate endpoint contract."""

    endpoint_id: str
    accepted: bool
    reason_code: str = "endpoint_contract_accepted"
    contract_schema: str = ENDPOINT_CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_reference(self.endpoint_id, "deployment_center_endpoint_id_invalid")
        if not isinstance(self.accepted, bool):
            raise DeploymentCenterError("deployment_center_contract_gate_invalid")
        _require_code(self.reason_code, "deployment_center_contract_reason_invalid")
        _require_contract_schema(
            self.contract_schema,
            "deployment_center_contract_schema_invalid",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "endpoint_id": self.endpoint_id,
            "accepted": self.accepted,
            "reason_code": self.reason_code,
            "contract_schema": self.contract_schema,
        }


@dataclass(frozen=True)
class ModelIdentityGate:
    """A fingerprint-only model identity comparison for one compute capability."""

    endpoint_id: str
    capability: str
    contract_version: str
    expected_identity_fingerprint: str
    observed_identity_fingerprint: str | None
    accepted: bool
    reason_code: str = "model_identity_accepted"

    def __post_init__(self) -> None:
        _require_reference(self.endpoint_id, "deployment_center_endpoint_id_invalid")
        expected_contract = _COMPUTE_CONTRACTS.get(self.capability)
        if expected_contract is None or self.contract_version != expected_contract:
            raise DeploymentCenterError("deployment_center_model_capability_invalid")
        _require_sha256(
            self.expected_identity_fingerprint,
            "deployment_center_expected_identity_invalid",
        )
        if self.observed_identity_fingerprint is not None:
            _require_sha256(
                self.observed_identity_fingerprint,
                "deployment_center_observed_identity_invalid",
            )
        if not isinstance(self.accepted, bool):
            raise DeploymentCenterError("deployment_center_model_gate_invalid")
        _require_code(self.reason_code, "deployment_center_model_reason_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "endpoint_id": self.endpoint_id,
            "capability": self.capability,
            "contract_version": self.contract_version,
            "expected_identity_fingerprint": self.expected_identity_fingerprint,
            "observed_identity_fingerprint": self.observed_identity_fingerprint,
            "accepted": self.accepted,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class EnrollmentReadiness:
    """Safe controller-owned readiness evidence for a future enrollment flow."""

    status: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.status not in _ENROLLMENT_STATUSES:
            raise DeploymentCenterError("deployment_center_enrollment_status_invalid")
        _require_code(self.reason_code, "deployment_center_enrollment_reason_invalid")

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "reason_code": self.reason_code}


@dataclass(frozen=True)
class ManifestTopologyProjection:
    """The redacted active V2 shape needed to calculate a safe manifest diff."""

    manifest_digest: str
    profile_id: str
    module_ids: tuple[str, ...]
    endpoint_ids: tuple[str, ...]
    compute_capability_kinds: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.manifest_digest, "deployment_center_active_manifest_digest_invalid")
        if profile_by_id(self.profile_id) is None:
            raise DeploymentCenterError("deployment_center_profile_unsupported")
        _require_unique_references(
            self.module_ids,
            "deployment_center_active_manifest_module_invalid",
        )
        _require_unique_references(
            self.endpoint_ids,
            "deployment_center_active_manifest_endpoint_invalid",
        )
        if len(set(self.compute_capability_kinds)) != len(self.compute_capability_kinds):
            raise DeploymentCenterError("deployment_center_active_manifest_capability_invalid")
        for kind in self.compute_capability_kinds:
            if kind not in _COMPUTE_CONTRACTS:
                raise DeploymentCenterError("deployment_center_active_manifest_capability_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_digest": self.manifest_digest,
            "profile": self.profile_id,
            "module_ids": list(self.module_ids),
            "endpoint_ids": list(self.endpoint_ids),
            "compute_capability_kinds": list(self.compute_capability_kinds),
        }


def _require_unique_references(values: tuple[str, ...], code: str) -> None:
    if len(set(values)) != len(values):
        raise DeploymentCenterError(code)
    for value in values:
        _require_reference(value, code)


@dataclass(frozen=True)
class ControllerState:
    """The safe projection of controller evidence needed for a browser preview.

    This intentionally contains no raw controller plan, backup location,
    endpoint address, credential, deployment receipt, or persistence result.
    Deployment receipts remain unavailable until the server-owned PR 5 flow.
    """

    observed_at: datetime
    freshness_seconds: int
    fresh: bool
    active_manifest_digest: str | None
    endpoint_contract_gates: tuple[EndpointContractGate, ...]
    model_identity_gates: tuple[ModelIdentityGate, ...]
    enrollment_readiness: EnrollmentReadiness
    active_manifest_projection: ManifestTopologyProjection | None = None

    def __post_init__(self) -> None:
        _require_utc(self.observed_at, "deployment_center_controller_observed_at_invalid")
        _require_positive_int(
            self.freshness_seconds,
            "deployment_center_controller_freshness_invalid",
        )
        if not isinstance(self.fresh, bool):
            raise DeploymentCenterError("deployment_center_controller_freshness_invalid")
        if self.active_manifest_digest is not None:
            _require_sha256(
                self.active_manifest_digest,
                "deployment_center_active_manifest_digest_invalid",
            )
        if self.active_manifest_projection is not None:
            if self.active_manifest_digest is None:
                raise DeploymentCenterError("deployment_center_active_manifest_projection_invalid")
            if self.active_manifest_projection.manifest_digest != self.active_manifest_digest:
                raise DeploymentCenterError("deployment_center_active_manifest_projection_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "observed_at": _timestamp(self.observed_at),
            "freshness_seconds": self.freshness_seconds,
            "fresh": self.fresh,
            "active_manifest_digest": self.active_manifest_digest,
            "enrollment_readiness": self.enrollment_readiness.to_dict(),
            "endpoint_contract_gates": [item.to_dict() for item in self.endpoint_contract_gates],
            "model_identity_gates": [item.to_dict() for item in self.model_identity_gates],
        }


class InstallationResolver(Protocol):
    """Resolve an opaque installation reference without exposing host paths."""

    def resolve(self, installation_ref: str) -> InstallationResolution:
        """Return logical labels for one already-known installation."""


class HostInspector(Protocol):
    """Read-only adapter for macOS, Linux, Windows, and WSL2 host facts."""

    def inspect(self, installation: InstallationResolution) -> HostInspection:
        """Return bounded host evidence without creating or changing host state."""


class ControllerStateAdapter(Protocol):
    """Read-only adapter over existing controller planning/state logic.

    An implementation may reuse existing controller planning internally, but
    it must return only the typed projection above.  Candidate comparison is
    performed inside ``DeploymentCenter`` only after the V2 parser resolves a
    preview request; the inspection seam never accepts a legacy manifest.
    """

    def inspect(self, installation: InstallationResolution) -> ControllerState:
        """Return current contract/model evidence without persistence."""


@dataclass(frozen=True)
class ProfileRecommendation:
    """An advisory profile recommendation; callers may submit another V2 candidate."""

    selected_profile_id: str | None
    recommended_profile_id: str
    reason_code: str
    advisory: bool = True

    def __post_init__(self) -> None:
        if self.selected_profile_id is not None and profile_by_id(self.selected_profile_id) is None:
            raise DeploymentCenterError("deployment_center_profile_unsupported")
        if profile_by_id(self.recommended_profile_id) is None:
            raise DeploymentCenterError("deployment_center_profile_unsupported")
        _require_code(self.reason_code, "deployment_center_profile_reason_invalid")
        if self.advisory is not True:
            raise DeploymentCenterError("deployment_center_profile_advisory_required")

    def to_dict(self) -> dict[str, object]:
        return {
            "selected_profile": self.selected_profile_id,
            "recommended_profile": self.recommended_profile_id,
            "reason_code": self.reason_code,
            "advisory": True,
        }


@dataclass(frozen=True)
class StoragePreflight:
    """One aggregated volume result with no local filesystem path."""

    volume_ref: str
    resource_refs: tuple[str, ...]
    total_bytes: int
    free_bytes: int
    planned_write_bytes: int
    post_install_free_bytes: int
    required_free_bytes: int
    ok: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "volume_ref": self.volume_ref,
            "resource_refs": list(self.resource_refs),
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "planned_write_bytes": self.planned_write_bytes,
            "post_install_free_bytes": self.post_install_free_bytes,
            "required_free_bytes": self.required_free_bytes,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class UpdateClassification:
    """A conservative, inspection-only future update category.

    PR 4 deliberately cannot inspect a persisted active manifest body or
    authorize a runtime operation.  It therefore emits only classifications
    supported by its redacted evidence, while retaining the complete closed
    vocabulary for the separately authorized PR 5 execution adapter.
    """

    kind: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.kind not in _UPDATE_CLASSES:
            raise DeploymentCenterError("deployment_center_update_class_invalid")
        _require_code(self.reason_code, "deployment_center_update_reason_invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "reason_code": self.reason_code,
            "authority": "inspection_only",
            "execution_status": "deferred_to_pr5",
        }


@dataclass(frozen=True)
class ManifestDiff:
    """A path-free structural V2 manifest diff for a planning-only preview."""

    availability: str
    reason_code: str
    candidate_manifest_digest: str
    active_manifest_digest: str | None
    profile_changed: bool | None
    added_module_ids: tuple[str, ...]
    removed_module_ids: tuple[str, ...]
    added_endpoint_ids: tuple[str, ...]
    removed_endpoint_ids: tuple[str, ...]
    added_compute_capabilities: tuple[str, ...]
    removed_compute_capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.availability not in _MANIFEST_DIFF_AVAILABILITY:
            raise DeploymentCenterError("deployment_center_manifest_diff_availability_invalid")
        _require_code(self.reason_code, "deployment_center_manifest_diff_reason_invalid")
        _require_sha256(
            self.candidate_manifest_digest,
            "deployment_center_candidate_manifest_digest_invalid",
        )
        if self.active_manifest_digest is not None:
            _require_sha256(
                self.active_manifest_digest,
                "deployment_center_active_manifest_digest_invalid",
            )
        if self.availability == "available":
            if not isinstance(self.profile_changed, bool):
                raise DeploymentCenterError("deployment_center_manifest_diff_invalid")
        elif self.profile_changed is not None:
            raise DeploymentCenterError("deployment_center_manifest_diff_invalid")
        for values in (
            self.added_module_ids,
            self.removed_module_ids,
            self.added_endpoint_ids,
            self.removed_endpoint_ids,
        ):
            _require_unique_references(values, "deployment_center_manifest_diff_invalid")
        for values in (self.added_compute_capabilities, self.removed_compute_capabilities):
            if len(set(values)) != len(values) or any(
                kind not in _COMPUTE_CONTRACTS for kind in values
            ):
                raise DeploymentCenterError("deployment_center_manifest_diff_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability,
            "reason_code": self.reason_code,
            "candidate_manifest_digest": self.candidate_manifest_digest,
            "active_manifest_digest": self.active_manifest_digest,
            "profile_changed": self.profile_changed,
            "added_module_ids": list(self.added_module_ids),
            "removed_module_ids": list(self.removed_module_ids),
            "added_endpoint_ids": list(self.added_endpoint_ids),
            "removed_endpoint_ids": list(self.removed_endpoint_ids),
            "added_compute_capabilities": list(self.added_compute_capabilities),
            "removed_compute_capabilities": list(self.removed_compute_capabilities),
        }


@dataclass(frozen=True)
class DeploymentInspection:
    """Fresh, safe evidence for rendering a non-authoritative browser view."""

    installation_ref: str
    profile_recommendation: ProfileRecommendation
    host: HostInspection
    controller_state: ControllerState

    @property
    def observed_at(self) -> datetime:
        """The oldest observation is the conservative combined observation time."""

        return min(self.host.observed_at, self.controller_state.observed_at)

    @property
    def freshness_seconds(self) -> int:
        """The shortest adapter freshness budget bounds the combined projection."""

        return min(self.host.freshness_seconds, self.controller_state.freshness_seconds)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DEPLOYMENT_CENTER_INSPECTION_SCHEMA_VERSION,
            "installation_ref": self.installation_ref,
            "observed_at": _timestamp(self.observed_at),
            "freshness_seconds": self.freshness_seconds,
            "profile_catalog": list(stable_profile_ids()),
            "profile_recommendation": self.profile_recommendation.to_dict(),
            "host": self.host.to_dict(),
            "controller_state": self.controller_state.to_dict(),
            "deployment_receipt": _receipt_projection(),
        }


@dataclass(frozen=True)
class DeploymentPreview:
    """A read-only, fail-closed deployment preview; never an execution request."""

    inspection: DeploymentInspection
    candidate: dict[str, object]
    manifest_comparison: str
    manifest_diff: ManifestDiff
    update_class: UpdateClassification
    plan_hash: str
    admissible: bool
    failure_codes: tuple[str, ...]
    resource_estimate: dict[str, int] | None
    storage_preflight: tuple[StoragePreflight, ...]
    endpoint_contract_gates: tuple[dict[str, object], ...]
    model_identity_gates: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DEPLOYMENT_CENTER_PREVIEW_SCHEMA_VERSION,
            "inspection": self.inspection.to_dict(),
            "candidate": self.candidate,
            "manifest_comparison": self.manifest_comparison,
            "manifest_diff": self.manifest_diff.to_dict(),
            "update_class": self.update_class.to_dict(),
            "plan_hash": self.plan_hash,
            "plan_hash_scope": "inspection_only",
            "plan_authorization": "deferred_to_pr5",
            "admissible": self.admissible,
            "failure_codes": list(self.failure_codes),
            "resource_estimate": self.resource_estimate,
            "storage_preflight": [item.to_dict() for item in self.storage_preflight],
            "endpoint_contract_gates": list(self.endpoint_contract_gates),
            "model_identity_gates": list(self.model_identity_gates),
            "deployment_receipt": _receipt_projection(),
        }


@dataclass(frozen=True)
class _Evaluation:
    """Private implementation state kept behind the two-operation interface."""

    request: DeploymentPreviewRequest
    candidate: ResolvedEndpointDeploymentPlan
    installation: InstallationResolution
    host: HostInspection
    controller_state: ControllerState
    inspection: DeploymentInspection


def _receipt_projection() -> dict[str, str]:
    """Make the current persistence boundary explicit in every response."""

    return {
        "availability": "unavailable",
        "persistence": "not_persisted",
        "state": "contract_unpersisted",
    }


class DeploymentCenter:
    """Compile deployment inspection and preview evidence behind two methods."""

    def __init__(
        self,
        *,
        installation_resolver: InstallationResolver,
        host_inspector: HostInspector,
        controller_state: ControllerStateAdapter,
    ) -> None:
        self._installation_resolver = installation_resolver
        self._host_inspector = host_inspector
        self._controller_state = controller_state

    def inspect(self, installation_ref: str) -> DeploymentInspection:
        """Return fresh host, catalog, status, and advisory profile evidence."""

        installation, host, controller_state = self._inspect_context(installation_ref)
        return DeploymentInspection(
            installation_ref=installation.installation_ref,
            profile_recommendation=_recommend_host_profile(host),
            host=host,
            controller_state=controller_state,
        )

    def preview(self, request: DeploymentPreviewRequest) -> DeploymentPreview:
        """Return a fresh, fail-closed read-only preview for one V2 candidate."""

        evaluation = self._evaluate(request)
        storage, storage_failures, resource_estimate = self._storage_preflight(evaluation)
        contract_gates, contract_failures = self._contract_gates(evaluation)
        model_gates, model_failures = self._model_identity_gates(evaluation)
        failures = _unique_codes(
            (
                *self._observation_failures(evaluation),
                *storage_failures,
                *contract_failures,
                *model_failures,
            )
        )
        return DeploymentPreview(
            inspection=evaluation.inspection,
            candidate=evaluation.candidate.browser_projection(),
            manifest_comparison=_manifest_comparison(
                evaluation.candidate.manifest_digest,
                evaluation.controller_state.active_manifest_digest,
            ),
            manifest_diff=_manifest_diff(evaluation),
            update_class=_classify_update(evaluation, failures),
            plan_hash=_inspection_plan_hash(evaluation),
            admissible=not failures,
            failure_codes=failures,
            resource_estimate=resource_estimate,
            storage_preflight=storage,
            endpoint_contract_gates=contract_gates,
            model_identity_gates=model_gates,
        )

    def _evaluate(self, request: DeploymentPreviewRequest) -> _Evaluation:
        if not isinstance(request, DeploymentPreviewRequest):
            raise DeploymentCenterError("deployment_center_preview_request_required")
        candidate = self._resolve_candidate(request.candidate_manifest)
        installation, host, controller_state = self._inspect_context(request.installation_ref)
        inspection = DeploymentInspection(
            installation_ref=installation.installation_ref,
            profile_recommendation=_recommend_candidate_profile(candidate),
            host=host,
            controller_state=controller_state,
        )
        return _Evaluation(
            request=request,
            candidate=candidate,
            installation=installation,
            host=host,
            controller_state=controller_state,
            inspection=inspection,
        )

    def _inspect_context(
        self,
        installation_ref: str,
    ) -> tuple[InstallationResolution, HostInspection, ControllerState]:
        _require_reference(
            installation_ref,
            "deployment_center_installation_reference_invalid",
        )
        installation = self._resolve_installation(installation_ref)
        host = self._inspect_host(installation)
        controller_state = self._inspect_controller_state(installation)
        return installation, host, controller_state

    @staticmethod
    def _resolve_candidate(manifest: Mapping[str, object]) -> ResolvedEndpointDeploymentPlan:
        try:
            return resolve_deployment_manifest_v2(manifest)
        except EndpointContractError:
            # Parser error codes can include a rejected user-supplied field name.
            # Keep the local-edge error stable rather than reflecting it.
            raise DeploymentCenterError("deployment_center_candidate_invalid") from None

    def _resolve_installation(self, installation_ref: str) -> InstallationResolution:
        try:
            resolution = self._installation_resolver.resolve(installation_ref)
        except DeploymentCenterError:
            raise
        except Exception:
            raise DeploymentCenterError("deployment_center_installation_unavailable") from None
        if not isinstance(resolution, InstallationResolution):
            raise DeploymentCenterError("deployment_center_installation_resolution_invalid")
        if resolution.installation_ref != installation_ref:
            raise DeploymentCenterError("deployment_center_installation_reference_mismatch")
        return resolution

    def _inspect_host(self, installation: InstallationResolution) -> HostInspection:
        try:
            observation = self._host_inspector.inspect(installation)
        except DeploymentCenterError:
            raise
        except Exception:
            raise DeploymentCenterError("deployment_center_host_inspection_unavailable") from None
        if not isinstance(observation, HostInspection):
            raise DeploymentCenterError("deployment_center_host_inspection_invalid")
        return observation

    def _inspect_controller_state(self, installation: InstallationResolution) -> ControllerState:
        try:
            state = self._controller_state.inspect(installation)
        except DeploymentCenterError:
            raise
        except Exception:
            raise DeploymentCenterError("deployment_center_controller_state_unavailable") from None
        if not isinstance(state, ControllerState):
            raise DeploymentCenterError("deployment_center_controller_state_invalid")
        return state

    @staticmethod
    def _observation_failures(evaluation: _Evaluation) -> tuple[str, ...]:
        failures: list[str] = []
        if not evaluation.host.fresh:
            failures.append("host_inspection_stale")
        if not evaluation.controller_state.fresh:
            failures.append("controller_state_stale")
        if not evaluation.host.container_runtime_ready:
            failures.append("container_runtime_unavailable")
        return tuple(failures)

    @staticmethod
    def _storage_preflight(
        evaluation: _Evaluation,
    ) -> tuple[tuple[StoragePreflight, ...], tuple[str, ...], dict[str, int] | None]:
        budget = evaluation.candidate.manifest.resource_budget
        if budget is None:
            return (), ("resource_budget_required",), None

        resource_estimate = budget.to_dict()
        requested_writes: dict[str, int] = {
            evaluation.installation.canonical_state_ref: (
                budget.lancedb_shadow_rebuild_bytes + budget.rollback_coexistence_bytes
            )
        }
        locations = evaluation.candidate.manifest.resource_locations
        failures: list[str] = []
        container_write = budget.image_layers_bytes + budget.image_unpack_bytes
        if container_write:
            if locations is None or locations.container_store is None:
                failures.append("container_store_reference_required")
            else:
                requested_writes[locations.container_store] = container_write
        if budget.model_cache_bytes:
            if locations is None or locations.model_cache is None:
                failures.append("model_cache_reference_required")
            else:
                requested_writes[locations.model_cache] = budget.model_cache_bytes

        storage_by_ref = {item.resource_ref: item for item in evaluation.host.storage}
        volumes: dict[str, dict[str, object]] = {}
        for resource_ref, planned_write in requested_writes.items():
            storage = storage_by_ref.get(resource_ref)
            if storage is None:
                failures.append("storage_evidence_missing")
                continue
            volume = volumes.setdefault(
                storage.volume_ref,
                {
                    "resource_refs": [],
                    "total_bytes": storage.total_bytes,
                    "free_bytes": storage.free_bytes,
                    "planned_write_bytes": 0,
                },
            )
            if (
                volume["total_bytes"] != storage.total_bytes
                or volume["free_bytes"] != storage.free_bytes
            ):
                failures.append("storage_volume_evidence_inconsistent")
                continue
            resource_refs = volume["resource_refs"]
            assert isinstance(resource_refs, list)
            resource_refs.append(resource_ref)
            volume["planned_write_bytes"] = int(volume["planned_write_bytes"]) + planned_write

        profile = profile_by_id(evaluation.candidate.profile_id)
        if profile is None:  # Defensive: the strict V2 resolver already checks this.
            raise DeploymentCenterError("deployment_center_profile_unsupported")
        reports: list[StoragePreflight] = []
        for volume_ref in sorted(volumes):
            volume = volumes[volume_ref]
            total = int(volume["total_bytes"])
            free = int(volume["free_bytes"])
            planned = int(volume["planned_write_bytes"])
            required = max(
                profile.resource_policy.minimum_free_bytes,
                math.ceil(total * profile.resource_policy.minimum_free_fraction),
            )
            post_install = max(0, free - planned)
            ok = post_install >= required
            if not ok:
                failures.append("post_install_disk_reserve_unmet")
            resource_refs = volume["resource_refs"]
            assert isinstance(resource_refs, list)
            reports.append(
                StoragePreflight(
                    volume_ref=volume_ref,
                    resource_refs=tuple(sorted(resource_refs)),
                    total_bytes=total,
                    free_bytes=free,
                    planned_write_bytes=planned,
                    post_install_free_bytes=post_install,
                    required_free_bytes=required,
                    ok=ok,
                )
            )
        return tuple(reports), tuple(failures), resource_estimate

    @staticmethod
    def _contract_gates(
        evaluation: _Evaluation,
    ) -> tuple[tuple[dict[str, object], ...], tuple[str, ...]]:
        by_endpoint: dict[str, list[EndpointContractGate]] = {}
        for gate in evaluation.controller_state.endpoint_contract_gates:
            by_endpoint.setdefault(gate.endpoint_id, []).append(gate)

        projections: list[dict[str, object]] = []
        failures: list[str] = []
        for endpoint in evaluation.candidate.endpoints:
            matching = by_endpoint.get(endpoint.endpoint_id, [])
            if len(matching) != 1:
                reason = (
                    "endpoint_contract_gate_missing"
                    if not matching
                    else "endpoint_contract_gate_duplicate"
                )
                failures.append(reason)
                projections.append(
                    {
                        "endpoint_id": endpoint.endpoint_id,
                        "accepted": False,
                        "reason_code": reason,
                        "contract_schema": None,
                    }
                )
                continue
            gate = matching[0]
            accepted = gate.accepted and gate.contract_schema == ENDPOINT_CONTRACT_SCHEMA_VERSION
            reason = (
                gate.reason_code
                if accepted
                else (
                    "endpoint_contract_schema_incompatible"
                    if gate.contract_schema != ENDPOINT_CONTRACT_SCHEMA_VERSION
                    else gate.reason_code
                )
            )
            if not accepted:
                failures.append(reason)
            projections.append(
                {
                    "endpoint_id": endpoint.endpoint_id,
                    "accepted": accepted,
                    "reason_code": reason,
                    "contract_schema": gate.contract_schema,
                }
            )
        return tuple(projections), tuple(failures)

    @staticmethod
    def _model_identity_gates(
        evaluation: _Evaluation,
    ) -> tuple[tuple[dict[str, object], ...], tuple[str, ...]]:
        by_capability: dict[tuple[str, str, str], list[ModelIdentityGate]] = {}
        for gate in evaluation.controller_state.model_identity_gates:
            key = (gate.endpoint_id, gate.capability, gate.contract_version)
            by_capability.setdefault(key, []).append(gate)

        projections: list[dict[str, object]] = []
        failures: list[str] = []
        for endpoint in evaluation.candidate.endpoints:
            for capability in endpoint.capabilities:
                key = (endpoint.endpoint_id, capability.kind, capability.contract_version)
                matching = by_capability.get(key, [])
                if len(matching) != 1:
                    reason = (
                        "model_identity_gate_missing"
                        if not matching
                        else "model_identity_gate_duplicate"
                    )
                    failures.append(reason)
                    projections.append(
                        {
                            "endpoint_id": endpoint.endpoint_id,
                            "capability": capability.kind,
                            "contract_version": capability.contract_version,
                            "expected_identity_fingerprint": None,
                            "observed_identity_fingerprint": None,
                            "accepted": False,
                            "reason_code": reason,
                        }
                    )
                    continue
                gate = matching[0]
                accepted = (
                    gate.accepted
                    and gate.observed_identity_fingerprint is not None
                    and gate.expected_identity_fingerprint == gate.observed_identity_fingerprint
                )
                reason = (
                    gate.reason_code
                    if accepted
                    else (
                        gate.reason_code
                        if not gate.accepted
                        else (
                            "model_identity_incompatible"
                            if gate.observed_identity_fingerprint is not None
                            else "model_identity_observation_missing"
                        )
                    )
                )
                if not accepted:
                    failures.append(reason)
                projections.append(
                    {
                        "endpoint_id": endpoint.endpoint_id,
                        "capability": capability.kind,
                        "contract_version": capability.contract_version,
                        "expected_identity_fingerprint": gate.expected_identity_fingerprint,
                        "observed_identity_fingerprint": gate.observed_identity_fingerprint,
                        "accepted": accepted,
                        "reason_code": reason,
                    }
                )
        return tuple(projections), tuple(failures)


def _recommend_host_profile(host: HostInspection) -> ProfileRecommendation:
    """Give inspection callers a catalog advisory without inventing a candidate."""

    return ProfileRecommendation(
        selected_profile_id=None,
        recommended_profile_id="local-all-in-one",
        reason_code=(
            "accelerator_catalog_available"
            if host.accelerator_available
            else "host_catalog_default"
        ),
    )


def _recommend_candidate_profile(
    candidate: ResolvedEndpointDeploymentPlan,
) -> ProfileRecommendation:
    """Choose a deterministic advisory profile without changing the candidate."""

    modules = set(candidate.module_ids)
    has_compute = any(endpoint.capabilities for endpoint in candidate.endpoints)
    if has_compute or "heterogeneous-inference-node" in modules:
        recommended = "split-accelerated"
        reason = "compute_endpoint_declared"
    elif "cloud-inference" in modules:
        recommended = "local-cloud"
        reason = "cloud_inference_selected"
    else:
        recommended = "local-all-in-one"
        reason = "local_default_selected"
    return ProfileRecommendation(
        selected_profile_id=candidate.profile_id,
        recommended_profile_id=recommended,
        reason_code=("profile_aligned" if candidate.profile_id == recommended else reason),
    )


def _manifest_comparison(candidate_digest: str, active_digest: str | None) -> str:
    if active_digest is None:
        return "active_manifest_unavailable"
    if active_digest == candidate_digest:
        return "candidate_matches_active"
    return "candidate_differs_from_active"


def _manifest_diff(evaluation: _Evaluation) -> ManifestDiff:
    """Return a redacted topology diff when the controller supplied one."""

    active = evaluation.controller_state.active_manifest_projection
    if active is None:
        return ManifestDiff(
            availability="unavailable",
            reason_code="active_manifest_projection_unavailable",
            candidate_manifest_digest=evaluation.candidate.manifest_digest,
            active_manifest_digest=evaluation.controller_state.active_manifest_digest,
            profile_changed=None,
            added_module_ids=(),
            removed_module_ids=(),
            added_endpoint_ids=(),
            removed_endpoint_ids=(),
            added_compute_capabilities=(),
            removed_compute_capabilities=(),
        )
    candidate_modules = set(evaluation.candidate.module_ids)
    candidate_endpoints = {endpoint.endpoint_id for endpoint in evaluation.candidate.endpoints}
    candidate_capabilities = {
        capability.kind
        for endpoint in evaluation.candidate.endpoints
        for capability in endpoint.capabilities
    }
    return ManifestDiff(
        availability="available",
        reason_code="manifest_diff_available",
        candidate_manifest_digest=evaluation.candidate.manifest_digest,
        active_manifest_digest=active.manifest_digest,
        profile_changed=active.profile_id != evaluation.candidate.profile_id,
        added_module_ids=tuple(sorted(candidate_modules - set(active.module_ids))),
        removed_module_ids=tuple(sorted(set(active.module_ids) - candidate_modules)),
        added_endpoint_ids=tuple(sorted(candidate_endpoints - set(active.endpoint_ids))),
        removed_endpoint_ids=tuple(sorted(set(active.endpoint_ids) - candidate_endpoints)),
        added_compute_capabilities=tuple(
            sorted(candidate_capabilities - set(active.compute_capability_kinds))
        ),
        removed_compute_capabilities=tuple(
            sorted(set(active.compute_capability_kinds) - candidate_capabilities)
        ),
    )


def _classify_update(
    evaluation: _Evaluation,
    failures: tuple[str, ...],
) -> UpdateClassification:
    """Classify only what the redacted PR 4 evidence can safely support.

    Even when the adapter provides a redacted V2 topology diff, it does not
    provide a persisted receipt or authorize a runtime operation. It would be
    deceptive to call a changed candidate a live apply, rolling restart,
    rebuild, or migration. Those labels remain in the closed taxonomy for the
    PR 5 adapter, which will own mutation authorization.
    """

    if failures:
        return UpdateClassification("manual-review", "preflight_not_admissible")
    active_digest = evaluation.controller_state.active_manifest_digest
    if active_digest == evaluation.candidate.manifest_digest:
        return UpdateClassification("no-change", "candidate_matches_active")
    enrollment = evaluation.controller_state.enrollment_readiness
    if enrollment.status == "required":
        return UpdateClassification("enrollment-required", enrollment.reason_code)
    if evaluation.controller_state.active_manifest_projection is None:
        return UpdateClassification("manual-review", "active_manifest_projection_unavailable")
    return UpdateClassification("manual-review", "active_manifest_diff_requires_pr5")


def _inspection_plan_hash(evaluation: _Evaluation) -> str:
    """Bind safe candidate and observed-state evidence, never authorization."""

    return _sha256(
        {
            "schema_version": DEPLOYMENT_CENTER_SCHEMA_VERSION,
            "installation_ref": evaluation.request.installation_ref,
            "manifest_digest": evaluation.candidate.manifest_digest,
            "selected_profile": evaluation.candidate.profile_id,
            "observed_state_fingerprint": _observed_state_fingerprint(evaluation),
        }
    )


def _observed_state_fingerprint(evaluation: _Evaluation) -> str:
    """Fingerprint the redacted observation that makes a preview drift-sensitive."""

    return _sha256(
        {
            "inspection": evaluation.inspection.to_dict(),
            "active_manifest_projection": (
                evaluation.controller_state.active_manifest_projection.to_dict()
                if evaluation.controller_state.active_manifest_projection is not None
                else None
            ),
        }
    )


def _unique_codes(codes: tuple[str, ...]) -> tuple[str, ...]:
    unique: list[str] = []
    for code in codes:
        _require_code(code, "deployment_center_failure_code_invalid")
        if code not in unique:
            unique.append(code)
    return tuple(unique)


__all__ = [
    "DEPLOYMENT_CENTER_INSPECTION_SCHEMA_VERSION",
    "DEPLOYMENT_CENTER_PREVIEW_SCHEMA_VERSION",
    "DEPLOYMENT_CENTER_SCHEMA_VERSION",
    "ControllerState",
    "ControllerStateAdapter",
    "DeploymentCenter",
    "DeploymentCenterError",
    "DeploymentPreviewRequest",
    "DeploymentInspection",
    "DeploymentPreview",
    "EndpointContractGate",
    "EnrollmentReadiness",
    "HostInspection",
    "HostInspector",
    "HostStorage",
    "InstallationResolution",
    "InstallationResolver",
    "ModelIdentityGate",
    "ManifestDiff",
    "ManifestTopologyProjection",
    "ProfileRecommendation",
    "StoragePreflight",
    "UpdateClassification",
]
