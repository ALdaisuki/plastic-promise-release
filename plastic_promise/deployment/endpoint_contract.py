"""Pure contracts for the three-endpoint deployment topology.

This module deliberately owns *only* the public deployment/compute contract.
It has no SQLite, Docker, HTTP, SSH, provider, or runtime imports.  The
``pp-server-backend`` adapts its decisions to the existing canonical SQLite
stores, while ``pp-local-edge`` receives a sanitised projection and
``pp-compute-node`` returns typed derived inference results.

Keeping that policy in one deep module prevents every scheduler, transport,
dashboard, and installer call site from having to re-implement protocol,
identity, heartbeat, resource, lease, and fencing rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from plastic_promise.endpoint_roles import (
    ENDPOINT_ROLES,
    PP_COMPUTE_NODE,
    PP_LOCAL_EDGE,
    PP_SERVER_BACKEND,
    EndpointRoleContractError,
    compute_package_manifest,
    endpoint_role_contract,
)

from .catalog import deployment_modules, profile_by_id

DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION = "plastic-promise-deployment/v2"
ENDPOINT_CONTRACT_SCHEMA_VERSION = "plastic-promise-endpoint-contract/v1"
DEPLOYMENT_RECEIPT_SCHEMA_VERSION = "plastic-promise-deployment-receipt/v1"
MANIFEST_REVISION_SCHEMA_VERSION = "plastic-promise-manifest-revision/v1"

_ENDPOINT_ROLES = frozenset(ENDPOINT_ROLES)
_COMPUTE_PACKAGE_MANIFEST = compute_package_manifest()
_COMPUTE_CAPABILITY_CONTRACTS = {
    capability.kind: capability.contract_version
    for capability in _COMPUTE_PACKAGE_MANIFEST.capabilities
}
_COMPUTE_INPUT_SCHEMAS = {
    capability.kind: capability.input_schema
    for capability in _COMPUTE_PACKAGE_MANIFEST.capabilities
}
_COMPUTE_RESULT_SCHEMAS = {
    capability.kind: capability.result_schema
    for capability in _COMPUTE_PACKAGE_MANIFEST.capabilities
}
_COMPUTE_CAPABILITIES = frozenset(_COMPUTE_CAPABILITY_CONTRACTS)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "profile",
        "modules",
        "endpoints",
        "resource_budget",
        "resource_locations",
    }
)
_ENDPOINT_FIELDS = frozenset(
    {
        "id",
        "role",
        "protocol",
        "capabilities",
        "max_concurrency",
        "transport_ref",
        "resource_policy_ref",
    }
)
_PROTOCOL_FIELDS = frozenset({"family", "major", "minor"})
_CAPABILITY_FIELDS = frozenset({"kind", "contract_version", "binding"})
_CAPABILITY_BINDING_FIELDS = frozenset(
    {
        "model_identity_fingerprint",
        "input_schema",
        "result_schema",
        "resources",
        "max_concurrency",
        "lease",
        "golden_probe",
    }
)
_CAPABILITY_RESOURCE_FIELDS = frozenset({"minimum_memory_mib", "minimum_model_cache_bytes"})
_CAPABILITY_LEASE_FIELDS = frozenset(
    {
        "timeout_seconds",
        "idempotency_key_schema",
        "cancel_supported",
        "terminal_reasons",
    }
)
_GOLDEN_PROBE_FIELDS = frozenset(
    {
        "input_schema",
        "result_schema",
        "probe_input_sha256",
        "expected_result_sha256",
    }
)
_MODULE_SELECTION_FIELDS = frozenset({"enabled", "acknowledge_high_risk"})
_RESOURCE_BUDGET_FIELDS = frozenset(
    {
        "image_layers_bytes",
        "image_unpack_bytes",
        "model_cache_bytes",
        "lancedb_shadow_rebuild_bytes",
        "rollback_coexistence_bytes",
    }
)
_RESOURCE_LOCATION_FIELDS = frozenset({"container_store", "model_cache"})
_DEPLOYMENT_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_ENDPOINT_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_SAFE_REFERENCE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_SAFE_SCOPE_REFERENCE = re.compile(r"^[a-z][a-z0-9:_-]{1,127}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_:-]{1,127}$")
_CAPABILITY_VERSION = re.compile(r"^[a-z][a-z0-9-]{1,63}/v[1-9][0-9]*$")
_PINNED_REVISION = re.compile(r"(?:[0-9a-f]{40}|sha256:[0-9a-f]{64})$")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
_SAFE_MODEL_FIELD = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SECRET_FIELD_TOKENS = frozenset(
    {
        "apikey",
        "authorization",
        "credential",
        "password",
        "privatekey",
        "secret",
        "token",
    }
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_UNPINNED_REVISIONS = frozenset({"latest", "main", "master", "stable", "head"})
_IDEMPOTENCY_KEY_SCHEMA = "sha256/v1"
_TERMINAL_REASONS = frozenset({"completed", "cancelled", "failed", "timed-out"})


class EndpointContractError(ValueError):
    """A stable, sanitised error from the endpoint-contract interface."""

    def __init__(
        self,
        code: str,
        *,
        category: str = "invalid",
        retryable: bool = False,
        retry_after_ms: int | None = None,
    ) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("endpoint_contract_error_code_invalid")
        if category not in {
            "invalid",
            "forbidden",
            "conflict",
            "stale",
            "capacity",
            "unavailable",
        }:
            raise ValueError("endpoint_contract_error_category_invalid")
        if retry_after_ms is not None and (
            isinstance(retry_after_ms, bool)
            or not isinstance(retry_after_ms, int)
            or retry_after_ms < 0
        ):
            raise ValueError("endpoint_contract_error_retry_after_invalid")
        self.code = code
        self.category = category
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        super().__init__(code)

    def public_json(self) -> dict[str, object]:
        """Return the safe error projection suitable for a UI or receipt."""

        return {
            "code": self.code,
            "category": self.category,
            "retryable": self.retryable,
            "retry_after_ms": self.retry_after_ms,
        }


@dataclass(frozen=True)
class EndpointAuthorityProfile:
    """Closed authority profile compiled solely from a declared endpoint role.

    Project identity, advertised capabilities, hello evidence, runtime fields,
    and caller-owned strings deliberately do not participate in compilation.
    They may constrain later admission, but they can never add an action.
    """

    role: str

    def __post_init__(self) -> None:
        try:
            endpoint_role_contract(self.role)
        except EndpointRoleContractError:
            raise EndpointContractError("endpoint_authority_role_invalid") from None

    @property
    def actions(self) -> tuple[str, ...]:
        """Return the deterministic closed action set for this role."""

        return endpoint_role_contract(self.role).actions

    @property
    def authorities(self) -> tuple[str, ...]:
        """Return the backward-compatible descriptive authority vocabulary."""

        return endpoint_role_contract(self.role).descriptive_authorities

    def allows(self, action: object) -> bool:
        """Fail closed for malformed, unknown, or role-forbidden actions."""

        return isinstance(action, str) and action in self.actions

    def require(self, action: object) -> None:
        """Require an action without reflecting untrusted input in the error."""

        if not self.allows(action):
            raise EndpointContractError("endpoint_authority_denied", category="forbidden")


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _sha256(payload: Mapping[str, object]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


def _require_mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EndpointContractError(code)
    return value


def _require_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EndpointContractError(code)
    return value


def _require_identifier(value: object, code: str, pattern: re.Pattern[str]) -> str:
    text = _require_string(value, code)
    if pattern.fullmatch(text) is None:
        raise EndpointContractError(code)
    return text


def _require_sha256(value: object, code: str) -> str:
    text = _require_string(value, code)
    if _SHA256.fullmatch(text) is None:
        raise EndpointContractError(code)
    return text


def _require_pinned_revision(value: object, code: str) -> str:
    text = _require_string(value, code)
    if (
        text.casefold() in _UNPINNED_REVISIONS
        or _PINNED_REVISION.fullmatch(text.casefold()) is None
    ):
        raise EndpointContractError(code)
    return text


def _require_capability_contract(
    capability: object,
    contract_version: object,
    *,
    capability_code: str,
) -> tuple[str, str]:
    """Require a closed V2 capability and its matching versioned schema."""

    kind = _require_string(capability, capability_code)
    expected_contract_version = _COMPUTE_CAPABILITY_CONTRACTS.get(kind)
    if expected_contract_version is None:
        raise EndpointContractError(capability_code)
    version = _require_identifier(
        contract_version,
        "endpoint_capability_contract_version_invalid",
        _CAPABILITY_VERSION,
    )
    if version != expected_contract_version:
        raise EndpointContractError("endpoint_capability_contract_version_mismatch")
    return kind, version


def _require_result_schema(capability: str, result_schema: object) -> str:
    """Require the closed body-free result schema for a V2 capability."""

    expected_result_schema = _COMPUTE_RESULT_SCHEMAS[capability]
    schema = _require_identifier(
        result_schema,
        "endpoint_result_schema_invalid",
        _CAPABILITY_VERSION,
    )
    if schema != expected_result_schema:
        raise EndpointContractError("endpoint_result_schema_capability_mismatch")
    return schema


def _require_input_schema(capability: str, input_schema: object) -> str:
    """Require the closed, body-free request schema for a V2 capability."""

    expected_input_schema = _COMPUTE_INPUT_SCHEMAS[capability]
    schema = _require_identifier(
        input_schema,
        "endpoint_input_schema_invalid",
        _CAPABILITY_VERSION,
    )
    if schema != expected_input_schema:
        raise EndpointContractError("endpoint_input_schema_capability_mismatch")
    return schema


def _require_terminal_reason(value: object, code: str) -> str:
    reason = _require_identifier(value, code, _SAFE_CODE)
    if reason not in _TERMINAL_REASONS:
        raise EndpointContractError(code)
    return reason


def _require_nonnegative_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EndpointContractError(code)
    return value


def _require_positive_int(value: object, code: str) -> int:
    value = _require_nonnegative_int(value, code)
    if value < 1:
        raise EndpointContractError(code)
    return value


def _require_utc(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EndpointContractError(code)
    return value.astimezone(timezone.utc)


def _utc_json(value: datetime) -> str:
    return _require_utc(value, "endpoint_timestamp_invalid").isoformat().replace("+00:00", "Z")


def _scan_for_secrets(value: object) -> None:
    """Reject credentials and credential-shaped values before any projection."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise EndpointContractError("endpoint_manifest_string_key_required")
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in _SECRET_FIELD_TOKENS:
                raise EndpointContractError("endpoint_manifest_secret_forbidden")
            _scan_for_secrets(nested)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _scan_for_secrets(item)
        return
    if isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise EndpointContractError("endpoint_manifest_secret_forbidden")


def _safe_model(value: object, code: str) -> str:
    text = _require_string(value, code)
    if _SAFE_MODEL.fullmatch(text) is None or "//" in text:
        raise EndpointContractError(code)
    return text


def _safe_model_field(value: object, code: str) -> str:
    text = _require_string(value, code)
    if _SAFE_MODEL_FIELD.fullmatch(text) is None:
        raise EndpointContractError(code)
    return text


@dataclass(frozen=True)
class EndpointProtocol:
    """A versioned typed protocol, independent of its private transport."""

    family: str
    major: int
    minor: int

    def __post_init__(self) -> None:
        _safe_model_field(self.family, "endpoint_protocol_family_invalid")
        _require_positive_int(self.major, "endpoint_protocol_major_invalid")
        _require_nonnegative_int(self.minor, "endpoint_protocol_minor_invalid")

    def to_dict(self) -> dict[str, object]:
        return {"family": self.family, "major": self.major, "minor": self.minor}


@dataclass(frozen=True)
class CapabilityResourceBinding:
    """The capability-local capacity floor, without host paths or device IDs."""

    minimum_memory_mib: int
    minimum_model_cache_bytes: int

    def __post_init__(self) -> None:
        _require_nonnegative_int(
            self.minimum_memory_mib,
            "endpoint_capability_minimum_memory_mib_invalid",
        )
        _require_nonnegative_int(
            self.minimum_model_cache_bytes,
            "endpoint_capability_minimum_model_cache_bytes_invalid",
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "minimum_memory_mib": self.minimum_memory_mib,
            "minimum_model_cache_bytes": self.minimum_model_cache_bytes,
        }


@dataclass(frozen=True)
class CapabilityLeaseBinding:
    """Lease and cancellation semantics declared with a capability binding."""

    timeout_seconds: int
    idempotency_key_schema: str
    cancel_supported: bool
    terminal_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not 1
            <= _require_positive_int(
                self.timeout_seconds,
                "endpoint_capability_timeout_seconds_invalid",
            )
            <= 3_600
        ):
            raise EndpointContractError("endpoint_capability_timeout_seconds_invalid")
        if self.idempotency_key_schema != _IDEMPOTENCY_KEY_SCHEMA:
            raise EndpointContractError("endpoint_capability_idempotency_schema_unsupported")
        if not isinstance(self.cancel_supported, bool):
            raise EndpointContractError("endpoint_capability_cancel_supported_invalid")
        if not self.terminal_reasons:
            raise EndpointContractError("endpoint_capability_terminal_reasons_required")
        terminal_reasons = {
            _require_terminal_reason(
                reason,
                "endpoint_capability_terminal_reason_invalid",
            )
            for reason in self.terminal_reasons
        }
        if len(terminal_reasons) != len(self.terminal_reasons):
            raise EndpointContractError("endpoint_capability_terminal_reason_duplicate")
        if "cancelled" in terminal_reasons and not self.cancel_supported:
            raise EndpointContractError("endpoint_capability_cancel_terminal_reason_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "idempotency_key_schema": self.idempotency_key_schema,
            "cancel_supported": self.cancel_supported,
            "terminal_reasons": list(self.terminal_reasons),
        }


@dataclass(frozen=True)
class GoldenProbeBinding:
    """Opaque, body-free golden-probe hashes for one capability binding."""

    input_schema: str
    result_schema: str
    probe_input_sha256: str
    expected_result_sha256: str

    def __post_init__(self) -> None:
        _require_identifier(
            self.input_schema,
            "endpoint_golden_probe_input_schema_invalid",
            _CAPABILITY_VERSION,
        )
        _require_identifier(
            self.result_schema,
            "endpoint_golden_probe_result_schema_invalid",
            _CAPABILITY_VERSION,
        )
        _require_sha256(
            self.probe_input_sha256,
            "endpoint_golden_probe_input_sha256_invalid",
        )
        _require_sha256(
            self.expected_result_sha256,
            "endpoint_golden_probe_result_sha256_invalid",
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "input_schema": self.input_schema,
            "result_schema": self.result_schema,
            "probe_input_sha256": self.probe_input_sha256,
            "expected_result_sha256": self.expected_result_sha256,
        }


@dataclass(frozen=True)
class CapabilityBinding:
    """One complete, portable capability acceptance contract.

    The binding carries only identifiers, resource floors, and body-free proof
    hashes.  It deliberately excludes credentials, endpoint addresses, model
    files, arbitrary request bodies, and execution instructions.
    """

    model_identity_fingerprint: str
    input_schema: str
    result_schema: str
    resources: CapabilityResourceBinding
    max_concurrency: int
    lease: CapabilityLeaseBinding
    golden_probe: GoldenProbeBinding

    def __post_init__(self) -> None:
        _require_sha256(
            self.model_identity_fingerprint,
            "endpoint_capability_identity_fingerprint_invalid",
        )
        if (
            not 1
            <= _require_positive_int(
                self.max_concurrency,
                "endpoint_capability_max_concurrency_invalid",
            )
            <= 64
        ):
            raise EndpointContractError("endpoint_capability_max_concurrency_invalid")

    def validate_for(self, capability: str) -> None:
        """Tie generic binding fields to one closed capability family."""

        _require_input_schema(capability, self.input_schema)
        _require_result_schema(capability, self.result_schema)
        if self.golden_probe.input_schema != self.input_schema:
            raise EndpointContractError("endpoint_golden_probe_input_schema_mismatch")
        if self.golden_probe.result_schema != self.result_schema:
            raise EndpointContractError("endpoint_golden_probe_result_schema_mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            "model_identity_fingerprint": self.model_identity_fingerprint,
            "input_schema": self.input_schema,
            "result_schema": self.result_schema,
            "resources": self.resources.to_dict(),
            "max_concurrency": self.max_concurrency,
            "lease": self.lease.to_dict(),
            "golden_probe": self.golden_probe.to_dict(),
        }

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True)
class EndpointCapability:
    """One closed, typed compute capability declared by a compute endpoint."""

    kind: str
    contract_version: str
    binding: CapabilityBinding | None = None

    def __post_init__(self) -> None:
        _require_capability_contract(
            self.kind,
            self.contract_version,
            capability_code="endpoint_capability_invalid",
        )
        if self.binding is not None:
            self.binding.validate_for(self.kind)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "contract_version": self.contract_version,
        }
        if self.binding is not None:
            payload["binding"] = self.binding.to_dict()
        return payload


@dataclass(frozen=True)
class EndpointDeclaration:
    """Static, secret-free endpoint placement and protocol declaration."""

    endpoint_id: str
    role: str
    protocol: EndpointProtocol
    capabilities: tuple[EndpointCapability, ...]
    transport_ref: str
    resource_policy_ref: str
    max_concurrency: int | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.endpoint_id, "endpoint_id_invalid", _ENDPOINT_ID)
        if self.role not in _ENDPOINT_ROLES:
            raise EndpointContractError("endpoint_role_invalid")
        _require_identifier(
            self.transport_ref, "endpoint_transport_reference_invalid", _SAFE_REFERENCE
        )
        _require_identifier(
            self.resource_policy_ref,
            "endpoint_resource_policy_reference_invalid",
            _SAFE_REFERENCE,
        )
        capability_keys = {(item.kind, item.contract_version) for item in self.capabilities}
        if len(capability_keys) != len(self.capabilities):
            raise EndpointContractError("endpoint_capability_duplicate")
        if self.role == PP_COMPUTE_NODE:
            if not self.capabilities:
                raise EndpointContractError("endpoint_compute_capability_required")
            if self.max_concurrency is None:
                raise EndpointContractError("endpoint_compute_max_concurrency_required")
            if not 1 <= self.max_concurrency <= 64:
                raise EndpointContractError("endpoint_compute_max_concurrency_invalid")
            for capability in self.capabilities:
                if (
                    capability.binding is not None
                    and capability.binding.max_concurrency > self.max_concurrency
                ):
                    raise EndpointContractError("endpoint_capability_concurrency_exceeds_endpoint")
        elif self.capabilities or self.max_concurrency is not None:
            raise EndpointContractError("endpoint_role_capability_invalid")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.endpoint_id,
            "role": self.role,
            "protocol": self.protocol.to_dict(),
            "capabilities": [item.to_dict() for item in self.capabilities],
            "transport_ref": self.transport_ref,
            "resource_policy_ref": self.resource_policy_ref,
        }
        if self.max_concurrency is not None:
            payload["max_concurrency"] = self.max_concurrency
        return payload

    def supports(self, capability: str, contract_version: str) -> bool:
        return self.capability_for(capability, contract_version) is not None

    def capability_for(
        self,
        capability: str,
        contract_version: str,
    ) -> EndpointCapability | None:
        return next(
            (
                item
                for item in self.capabilities
                if item.kind == capability and item.contract_version == contract_version
            ),
            None,
        )


@dataclass(frozen=True)
class ResourceBudgetEvidence:
    """A complete, host-path-free estimate used by later preflight adapters."""

    image_layers_bytes: int
    image_unpack_bytes: int
    model_cache_bytes: int
    lancedb_shadow_rebuild_bytes: int
    rollback_coexistence_bytes: int

    def __post_init__(self) -> None:
        for field in _RESOURCE_BUDGET_FIELDS:
            _require_nonnegative_int(
                getattr(self, field), f"endpoint_resource_budget_invalid:{field}"
            )

    @property
    def planned_write_bytes(self) -> int:
        return sum(getattr(self, field) for field in _RESOURCE_BUDGET_FIELDS)

    def manifest_payload(self) -> dict[str, int]:
        """Return exactly the fields accepted by the V2 manifest parser."""

        return {
            "image_layers_bytes": self.image_layers_bytes,
            "image_unpack_bytes": self.image_unpack_bytes,
            "model_cache_bytes": self.model_cache_bytes,
            "lancedb_shadow_rebuild_bytes": self.lancedb_shadow_rebuild_bytes,
            "rollback_coexistence_bytes": self.rollback_coexistence_bytes,
        }

    def to_dict(self) -> dict[str, int]:
        """Return the manifest fields plus the derived planning total for projections."""

        return {
            **self.manifest_payload(),
            "planned_write_bytes": self.planned_write_bytes,
        }


@dataclass(frozen=True)
class ResourceLocationReferences:
    """Opaque host-managed resource labels; never local paths or SSH targets."""

    container_store: str | None
    model_cache: str | None

    def __post_init__(self) -> None:
        for field in _RESOURCE_LOCATION_FIELDS:
            value = getattr(self, field)
            if value is not None:
                _require_identifier(
                    value, f"endpoint_resource_location_invalid:{field}", _SAFE_REFERENCE
                )

    def to_dict(self) -> dict[str, str | None]:
        return {"container_store": self.container_store, "model_cache": self.model_cache}


@dataclass(frozen=True)
class EndpointManifestV2:
    """The versioned, secret-free topology declaration for one deployment."""

    deployment_id: str
    profile_id: str
    module_ids: tuple[str, ...]
    endpoints: tuple[EndpointDeclaration, ...]
    resource_budget: ResourceBudgetEvidence | None
    resource_locations: ResourceLocationReferences | None
    schema_version: str = DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION:
            raise EndpointContractError("endpoint_manifest_schema_unsupported")
        _require_identifier(self.deployment_id, "endpoint_deployment_id_invalid", _DEPLOYMENT_ID)
        profile = profile_by_id(self.profile_id)
        if profile is None:
            raise EndpointContractError("endpoint_profile_unsupported")
        catalog = {module.id for module in deployment_modules()}
        if len(set(self.module_ids)) != len(self.module_ids):
            raise EndpointContractError("endpoint_module_duplicate")
        for module_id in self.module_ids:
            _require_identifier(module_id, "endpoint_module_id_invalid", _SAFE_REFERENCE)
            if module_id not in catalog:
                raise EndpointContractError("endpoint_module_unsupported")
        if not set(profile.base_modules).issubset(self.module_ids):
            raise EndpointContractError("endpoint_core_module_implicit")
        if not self.endpoints:
            raise EndpointContractError("endpoint_manifest_endpoints_required")
        endpoint_ids = {endpoint.endpoint_id for endpoint in self.endpoints}
        if len(endpoint_ids) != len(self.endpoints):
            raise EndpointContractError("endpoint_id_duplicate")
        counts = {
            role: sum(endpoint.role == role for endpoint in self.endpoints)
            for role in _ENDPOINT_ROLES
        }
        if counts[PP_LOCAL_EDGE] != 1 or counts[PP_SERVER_BACKEND] != 1:
            raise EndpointContractError("endpoint_role_assignment_invalid")
        if self.profile_id == "split-accelerated" and counts[PP_COMPUTE_NODE] < 1:
            raise EndpointContractError("endpoint_compute_endpoint_required")
        if self.resource_budget is not None:
            self._validate_resource_budget()

    def _validate_resource_budget(self) -> None:
        assert self.resource_budget is not None
        required = {"lancedb_shadow_rebuild_bytes", "rollback_coexistence_bytes"}
        if self.profile_id == "split-accelerated":
            required.update({"image_layers_bytes", "image_unpack_bytes"})
        if any(endpoint.role == PP_COMPUTE_NODE for endpoint in self.endpoints):
            required.add("model_cache_bytes")
        for field in sorted(required):
            if getattr(self.resource_budget, field) <= 0:
                raise EndpointContractError(f"endpoint_resource_budget_estimate_required:{field}")
        if (
            self.resource_budget.image_layers_bytes + self.resource_budget.image_unpack_bytes > 0
            and (self.resource_locations is None or self.resource_locations.container_store is None)
        ):
            raise EndpointContractError("endpoint_resource_location_container_store_required")
        if self.resource_budget.model_cache_bytes > 0 and (
            self.resource_locations is None or self.resource_locations.model_cache is None
        ):
            raise EndpointContractError("endpoint_resource_location_model_cache_required")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "deployment_id": self.deployment_id,
            "profile": self.profile_id,
            "modules": self._canonical_module_selections(),
            "endpoints": [endpoint.to_dict() for endpoint in self.endpoints],
            "resource_budget": (
                self.resource_budget.manifest_payload() if self.resource_budget else None
            ),
            "resource_locations": self.resource_locations.to_dict()
            if self.resource_locations
            else None,
        }

    def _canonical_module_selections(self) -> dict[str, dict[str, bool]]:
        """Render the effective optional-module choices in parser input form.

        Core modules remain implicit because a V2 manifest may not select them
        directly. Dependencies introduced by a selected optional module are
        represented as enabled choices as well, producing a deterministic,
        re-parseable effective manifest without carrying an untyped raw input
        mapping through the resolved contract.
        """

        catalog = {module.id: module for module in deployment_modules()}
        selections: dict[str, dict[str, bool]] = {}
        for module_id in self.module_ids:
            module = catalog[module_id]
            if module.risk_tier == "core":
                continue
            selection = {"enabled": True}
            if module.risk_tier == "high-risk":
                selection["acknowledge_high_risk"] = True
            selections[module_id] = selection
        return selections

    @property
    def manifest_digest(self) -> str:
        return _sha256(self.canonical_payload())


@dataclass(frozen=True)
class ResolvedEndpointDeploymentPlan:
    """A resolved topology contract, distinct from operation-bound ``DeploymentPlan``."""

    manifest: EndpointManifestV2
    manifest_digest: str
    canonical_sqlite_owner: str
    lancedb_promotion_owner: str
    receipt_persistence_owner: str

    def __post_init__(self) -> None:
        if self.manifest_digest != self.manifest.manifest_digest:
            raise EndpointContractError("endpoint_manifest_digest_mismatch")
        expected = self.endpoint_for_role(PP_SERVER_BACKEND).endpoint_id
        if (
            self.canonical_sqlite_owner != expected
            or self.lancedb_promotion_owner != expected
            or self.receipt_persistence_owner != expected
        ):
            raise EndpointContractError("endpoint_canonical_ownership_invalid")

    @property
    def deployment_id(self) -> str:
        return self.manifest.deployment_id

    @property
    def profile_id(self) -> str:
        return self.manifest.profile_id

    @property
    def module_ids(self) -> tuple[str, ...]:
        return self.manifest.module_ids

    @property
    def endpoints(self) -> tuple[EndpointDeclaration, ...]:
        return self.manifest.endpoints

    def endpoint_for_role(self, role: str) -> EndpointDeclaration:
        matches = [endpoint for endpoint in self.endpoints if endpoint.role == role]
        if len(matches) != 1:
            raise EndpointContractError("endpoint_role_assignment_invalid")
        return matches[0]

    def endpoint_by_id(self, endpoint_id: str) -> EndpointDeclaration | None:
        return next((item for item in self.endpoints if item.endpoint_id == endpoint_id), None)

    def authority_profile_for(self, endpoint_id: str) -> EndpointAuthorityProfile:
        """Compile the endpoint authority profile from its declared role only."""

        endpoint = self.endpoint_by_id(endpoint_id)
        if endpoint is None:
            raise EndpointContractError("endpoint_not_declared", category="unavailable")
        return EndpointAuthorityProfile(endpoint.role)

    def authorities_for(self, endpoint_id: str) -> tuple[str, ...]:
        """Return the backward-compatible descriptive authority projection."""

        return self.authority_profile_for(endpoint_id).authorities

    def browser_projection(self) -> dict[str, object]:
        """Return a plan view with no paths, addresses, credentials, or leases."""

        return {
            "schema_version": ENDPOINT_CONTRACT_SCHEMA_VERSION,
            "deployment_id": self.deployment_id,
            "profile": self.profile_id,
            "manifest_digest": self.manifest_digest,
            "modules": list(self.module_ids),
            "endpoints": [
                {
                    "id": endpoint.endpoint_id,
                    "role": endpoint.role,
                    "protocol": endpoint.protocol.to_dict(),
                    "capabilities": [item.to_dict() for item in endpoint.capabilities],
                    "max_concurrency": endpoint.max_concurrency,
                    "authorities": list(self.authorities_for(endpoint.endpoint_id)),
                    "actions": list(self.authority_profile_for(endpoint.endpoint_id).actions),
                }
                for endpoint in self.endpoints
            ],
            "canonical_sqlite_owner": self.canonical_sqlite_owner,
        }


@dataclass(frozen=True)
class EmbeddingIdentity:
    """Complete vector-space identity required for a shared index generation."""

    model: str
    revision: str
    dimension: int
    normalization: str
    metric: str
    tokenization: str
    pooling: str
    artifact_sha256: str
    golden_vector_sha256: str

    def __post_init__(self) -> None:
        _safe_model(self.model, "endpoint_embedding_model_invalid")
        _require_pinned_revision(self.revision, "endpoint_embedding_revision_not_pinned")
        if (
            not 1
            <= _require_positive_int(self.dimension, "endpoint_embedding_dimension_invalid")
            <= 65_536
        ):
            raise EndpointContractError("endpoint_embedding_dimension_invalid")
        for field in ("normalization", "metric", "tokenization", "pooling"):
            _safe_model_field(getattr(self, field), f"endpoint_embedding_{field}_invalid")
        _require_sha256(self.artifact_sha256, "endpoint_embedding_artifact_sha256_invalid")
        _require_sha256(
            self.golden_vector_sha256, "endpoint_embedding_golden_vector_sha256_invalid"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "revision": self.revision,
            "dimension": self.dimension,
            "normalization": self.normalization,
            "metric": self.metric,
            "tokenization": self.tokenization,
            "pooling": self.pooling,
            "artifact_sha256": self.artifact_sha256,
            "golden_vector_sha256": self.golden_vector_sha256,
        }

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True)
class RerankIdentity:
    """Complete scoring identity for a governed rerank capability."""

    model: str
    revision: str
    artifact_sha256: str
    scoring_schema: str

    def __post_init__(self) -> None:
        _safe_model(self.model, "endpoint_rerank_model_invalid")
        _require_pinned_revision(self.revision, "endpoint_rerank_revision_not_pinned")
        _require_sha256(self.artifact_sha256, "endpoint_rerank_artifact_sha256_invalid")
        _require_identifier(
            self.scoring_schema,
            "endpoint_rerank_scoring_schema_invalid",
            _CAPABILITY_VERSION,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "model": self.model,
            "revision": self.revision,
            "artifact_sha256": self.artifact_sha256,
            "scoring_schema": self.scoring_schema,
        }

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True)
class EndpointIdentityEvidence:
    """A closed identity document; missing fields never imply compatibility."""

    embedding: EmbeddingIdentity | None = None
    rerank: RerankIdentity | None = None
    schema_version: str = "endpoint-identity/v1"

    def __post_init__(self) -> None:
        if self.schema_version != "endpoint-identity/v1":
            raise EndpointContractError("endpoint_identity_schema_unsupported")
        if self.embedding is None and self.rerank is None:
            raise EndpointContractError("endpoint_identity_evidence_incomplete")

    def fingerprint_for(self, capability: str) -> str | None:
        if capability == "embedding" and self.embedding is not None:
            return self.embedding.fingerprint
        if capability == "rerank" and self.rerank is not None:
            return self.rerank.fingerprint
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "embedding": self.embedding.to_dict() if self.embedding else None,
            "rerank": self.rerank.to_dict() if self.rerank else None,
        }


@dataclass(frozen=True)
class EndpointHello:
    """The typed, static node attestation observed over a private transport."""

    endpoint_id: str
    role: str
    protocol: EndpointProtocol
    capabilities: tuple[EndpointCapability, ...]
    identity: EndpointIdentityEvidence | None

    def __post_init__(self) -> None:
        _require_identifier(self.endpoint_id, "endpoint_id_invalid", _ENDPOINT_ID)
        if self.role not in _ENDPOINT_ROLES:
            raise EndpointContractError("endpoint_role_invalid")
        if self.role == PP_COMPUTE_NODE and self.identity is None:
            raise EndpointContractError("endpoint_identity_evidence_incomplete")
        if self.role != PP_COMPUTE_NODE and (self.capabilities or self.identity is not None):
            raise EndpointContractError("endpoint_role_capability_invalid")
        capability_keys = {(item.kind, item.contract_version) for item in self.capabilities}
        if len(capability_keys) != len(self.capabilities):
            raise EndpointContractError("endpoint_capability_duplicate")

    def capability_for(
        self,
        capability: str,
        contract_version: str,
    ) -> EndpointCapability | None:
        return next(
            (
                item
                for item in self.capabilities
                if item.kind == capability and item.contract_version == contract_version
            ),
            None,
        )


@dataclass(frozen=True)
class EndpointHeartbeat:
    """Server-observed freshness evidence; node clocks are not authoritative."""

    endpoint_id: str
    boot_id: str
    sequence: int
    server_observed_at: datetime
    ttl_seconds: int

    def __post_init__(self) -> None:
        _require_identifier(self.endpoint_id, "endpoint_id_invalid", _ENDPOINT_ID)
        _require_identifier(self.boot_id, "endpoint_boot_id_invalid", _SAFE_REFERENCE)
        _require_positive_int(self.sequence, "endpoint_heartbeat_sequence_invalid")
        _require_utc(self.server_observed_at, "endpoint_heartbeat_timestamp_invalid")
        if (
            not 1
            <= _require_positive_int(self.ttl_seconds, "endpoint_heartbeat_ttl_invalid")
            <= 3_600
        ):
            raise EndpointContractError("endpoint_heartbeat_ttl_invalid")

    def is_fresh(self, observed_at: datetime) -> bool:
        observed_at = _require_utc(observed_at, "endpoint_observation_timestamp_invalid")
        server_observed = _require_utc(
            self.server_observed_at, "endpoint_heartbeat_timestamp_invalid"
        )
        return (
            server_observed <= observed_at <= server_observed + timedelta(seconds=self.ttl_seconds)
        )


@dataclass(frozen=True)
class AcceleratorResource:
    """A bounded accelerator observation with no serial number or device path."""

    kind: str
    memory_total_mib: int
    memory_free_mib: int
    utilization_percent: int

    def __post_init__(self) -> None:
        _safe_model_field(self.kind, "endpoint_accelerator_kind_invalid")
        total = _require_nonnegative_int(
            self.memory_total_mib, "endpoint_accelerator_memory_total_invalid"
        )
        free = _require_nonnegative_int(
            self.memory_free_mib, "endpoint_accelerator_memory_free_invalid"
        )
        if free > total:
            raise EndpointContractError("endpoint_accelerator_memory_free_invalid")
        utilization = _require_nonnegative_int(
            self.utilization_percent, "endpoint_accelerator_utilization_invalid"
        )
        if utilization > 100:
            raise EndpointContractError("endpoint_accelerator_utilization_invalid")


@dataclass(frozen=True)
class EndpointResourceReport:
    """Bounded capacity report consumed by admission, never by direct execution."""

    report_generation: int
    queue_depth: int
    active_lease_count: int
    available_slots: int
    max_concurrency: int
    memory_total_mib: int
    memory_free_mib: int
    model_cache_free_bytes: int
    accelerators: tuple[AcceleratorResource, ...] = ()

    def __post_init__(self) -> None:
        _require_positive_int(self.report_generation, "endpoint_resource_report_generation_invalid")
        for field in (
            "queue_depth",
            "active_lease_count",
            "available_slots",
            "max_concurrency",
            "memory_total_mib",
            "memory_free_mib",
            "model_cache_free_bytes",
        ):
            _require_nonnegative_int(
                getattr(self, field), f"endpoint_resource_report_invalid:{field}"
            )
        if (
            self.available_slots > self.max_concurrency
            or self.memory_free_mib > self.memory_total_mib
        ):
            raise EndpointContractError("endpoint_resource_report_invalid")


@dataclass(frozen=True)
class EndpointObservation:
    """One server-side observation of an endpoint's typed contract and capacity."""

    hello: EndpointHello
    heartbeat: EndpointHeartbeat
    resources: EndpointResourceReport | None

    def __post_init__(self) -> None:
        if self.hello.endpoint_id != self.heartbeat.endpoint_id:
            raise EndpointContractError("endpoint_observation_endpoint_mismatch")
        if self.hello.role == PP_COMPUTE_NODE and self.resources is None:
            raise EndpointContractError("endpoint_resource_report_required")
        if self.hello.role != PP_COMPUTE_NODE and self.resources is not None:
            raise EndpointContractError("endpoint_resource_report_unexpected")


@dataclass(frozen=True)
class EndpointRequirement:
    """Server-derived suitability requirement for a typed compute operation."""

    capability: str
    contract_version: str
    protocol: EndpointProtocol
    required_identity: EndpointIdentityEvidence
    allowed_endpoint_ids: tuple[str, ...] = ()
    pinned_endpoint_id: str | None = None
    minimum_available_slots: int = 1
    capability_binding: CapabilityBinding | None = None

    def __post_init__(self) -> None:
        _require_capability_contract(
            self.capability,
            self.contract_version,
            capability_code="endpoint_capability_schema_unsupported",
        )
        if self.required_identity.fingerprint_for(self.capability) is None:
            raise EndpointContractError("endpoint_identity_evidence_incomplete")
        if self.capability_binding is not None:
            self.capability_binding.validate_for(self.capability)
            if (
                self.required_identity.fingerprint_for(self.capability)
                != self.capability_binding.model_identity_fingerprint
            ):
                raise EndpointContractError("endpoint_capability_identity_requirement_mismatch")
        allowed = set()
        for endpoint_id in self.allowed_endpoint_ids:
            _require_identifier(endpoint_id, "endpoint_allowed_endpoint_invalid", _ENDPOINT_ID)
            allowed.add(endpoint_id)
        if len(allowed) != len(self.allowed_endpoint_ids):
            raise EndpointContractError("endpoint_allowed_endpoint_duplicate")
        if self.pinned_endpoint_id is not None:
            _require_identifier(
                self.pinned_endpoint_id, "endpoint_pinned_endpoint_invalid", _ENDPOINT_ID
            )
            if self.allowed_endpoint_ids and self.pinned_endpoint_id not in allowed:
                raise EndpointContractError("endpoint_pinned_endpoint_not_allowed")
        _require_positive_int(
            self.minimum_available_slots, "endpoint_minimum_available_slots_invalid"
        )


@dataclass(frozen=True)
class EndpointBinding:
    """The safe admission result consumed by a later durable-work adapter."""

    manifest_digest: str
    endpoint_id: str
    capability: str
    contract_version: str
    protocol: EndpointProtocol
    identity_fingerprint: str
    heartbeat_sequence: int
    resource_report_generation: int
    bound_at: datetime
    capability_binding: CapabilityBinding | None = None

    def __post_init__(self) -> None:
        _require_sha256(self.manifest_digest, "endpoint_manifest_digest_invalid")
        _require_identifier(self.endpoint_id, "endpoint_id_invalid", _ENDPOINT_ID)
        _require_capability_contract(
            self.capability,
            self.contract_version,
            capability_code="endpoint_capability_schema_unsupported",
        )
        _require_sha256(self.identity_fingerprint, "endpoint_identity_fingerprint_invalid")
        _require_positive_int(self.heartbeat_sequence, "endpoint_heartbeat_sequence_invalid")
        _require_positive_int(
            self.resource_report_generation, "endpoint_resource_report_generation_invalid"
        )
        _require_utc(self.bound_at, "endpoint_binding_timestamp_invalid")
        if self.capability_binding is not None:
            self.capability_binding.validate_for(self.capability)
            if self.capability_binding.model_identity_fingerprint != self.identity_fingerprint:
                raise EndpointContractError("endpoint_capability_identity_binding_mismatch")


@dataclass(frozen=True)
class ManifestRevisionRecord:
    """Server-owned revision schema; persistence is introduced in a later migration PR."""

    deployment_id: str
    revision: int
    manifest_digest: str
    parent_manifest_digest: str | None
    created_at: datetime
    status: str
    schema_version: str = MANIFEST_REVISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MANIFEST_REVISION_SCHEMA_VERSION:
            raise EndpointContractError("endpoint_manifest_revision_schema_unsupported")
        _require_identifier(self.deployment_id, "endpoint_deployment_id_invalid", _DEPLOYMENT_ID)
        _require_positive_int(self.revision, "endpoint_manifest_revision_invalid")
        _require_sha256(self.manifest_digest, "endpoint_manifest_digest_invalid")
        if self.parent_manifest_digest is not None:
            _require_sha256(self.parent_manifest_digest, "endpoint_parent_manifest_digest_invalid")
        if self.status not in {"staged", "active", "superseded", "aborted"}:
            raise EndpointContractError("endpoint_manifest_revision_status_invalid")
        _require_utc(self.created_at, "endpoint_manifest_revision_timestamp_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "deployment_id": self.deployment_id,
            "revision": self.revision,
            "manifest_digest": self.manifest_digest,
            "parent_manifest_digest": self.parent_manifest_digest,
            "created_at": _utc_json(self.created_at),
            "status": self.status,
            "owner": PP_SERVER_BACKEND,
        }


@dataclass(frozen=True)
class DeploymentReceipt:
    """Sanitised event schema owned and later persisted by the server backend."""

    receipt_id: str
    manifest_digest: str
    endpoint_id: str | None
    event: str
    outcome: str
    reason_code: str
    observed_at: datetime
    fencing_generation: int | None = None
    schema_version: str = DEPLOYMENT_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DEPLOYMENT_RECEIPT_SCHEMA_VERSION:
            raise EndpointContractError("endpoint_deployment_receipt_schema_unsupported")
        _require_identifier(self.receipt_id, "endpoint_receipt_id_invalid", _SAFE_REFERENCE)
        _require_sha256(self.manifest_digest, "endpoint_manifest_digest_invalid")
        if self.endpoint_id is not None:
            _require_identifier(self.endpoint_id, "endpoint_id_invalid", _ENDPOINT_ID)
        if self.event not in {"plan-resolved", "endpoint-admitted", "compute-completed"}:
            raise EndpointContractError("endpoint_receipt_event_invalid")
        if self.outcome not in {"accepted", "rejected"}:
            raise EndpointContractError("endpoint_receipt_outcome_invalid")
        _require_identifier(self.reason_code, "endpoint_receipt_reason_invalid", _SAFE_CODE)
        if self.fencing_generation is not None:
            _require_positive_int(self.fencing_generation, "endpoint_fencing_generation_invalid")
        _require_utc(self.observed_at, "endpoint_receipt_timestamp_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "manifest_digest": self.manifest_digest,
            "endpoint_id": self.endpoint_id,
            "event": self.event,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "observed_at": _utc_json(self.observed_at),
            "fencing_generation": self.fencing_generation,
            "owner": PP_SERVER_BACKEND,
        }


@dataclass(frozen=True)
class EndpointAdmission:
    """Admission decision plus the evidence record that a server may persist."""

    accepted: bool
    reason_code: str
    receipt: DeploymentReceipt
    binding: EndpointBinding | None = None
    retryable: bool = False
    quarantine_recommended: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.reason_code, "endpoint_admission_reason_invalid", _SAFE_CODE)
        if self.accepted != (self.binding is not None):
            raise EndpointContractError("endpoint_admission_binding_invalid")
        if self.receipt.outcome != ("accepted" if self.accepted else "rejected"):
            raise EndpointContractError("endpoint_admission_receipt_invalid")


@dataclass(frozen=True)
class ComputeLease:
    """A wire-safe projection of an existing durable derived-work lease."""

    lease_id: str
    job_id: str
    project_id: str
    endpoint_id: str
    manifest_digest: str
    fencing_generation: int
    capability: str
    contract_version: str
    required_identity_fingerprint: str
    result_schema: str
    idempotency_key: str
    issued_at: datetime
    expires_at: datetime
    input_schema: str | None = None
    capability_binding_fingerprint: str | None = None

    def __post_init__(self) -> None:
        for field in ("lease_id", "job_id"):
            _require_identifier(
                getattr(self, field), f"endpoint_compute_{field}_invalid", _SAFE_REFERENCE
            )
        _require_identifier(
            self.project_id,
            "endpoint_compute_project_id_invalid",
            _SAFE_SCOPE_REFERENCE,
        )
        _require_identifier(self.endpoint_id, "endpoint_id_invalid", _ENDPOINT_ID)
        _require_sha256(self.manifest_digest, "endpoint_manifest_digest_invalid")
        _require_positive_int(self.fencing_generation, "endpoint_fencing_generation_invalid")
        _require_capability_contract(
            self.capability,
            self.contract_version,
            capability_code="endpoint_capability_schema_unsupported",
        )
        _require_sha256(
            self.required_identity_fingerprint,
            "endpoint_identity_fingerprint_invalid",
        )
        _require_result_schema(self.capability, self.result_schema)
        _require_sha256(self.idempotency_key, "endpoint_idempotency_key_invalid")
        issued_at = _require_utc(self.issued_at, "endpoint_lease_timestamp_invalid")
        expires_at = _require_utc(self.expires_at, "endpoint_lease_timestamp_invalid")
        if expires_at <= issued_at:
            raise EndpointContractError("endpoint_lease_expiry_invalid")
        if self.input_schema is not None:
            _require_input_schema(self.capability, self.input_schema)
        if self.capability_binding_fingerprint is not None:
            _require_sha256(
                self.capability_binding_fingerprint,
                "endpoint_capability_binding_fingerprint_invalid",
            )


@dataclass(frozen=True)
class ComputeFence:
    """Current server-owned fencing value for one durable derived-work job.

    The existing durable-work store supplies this narrow input.  PR 2 does not
    persist or issue leases; it only rejects a completion that no longer owns
    the current fence for its job.
    """

    job_id: str
    fencing_generation: int

    def __post_init__(self) -> None:
        _require_identifier(self.job_id, "endpoint_compute_job_id_invalid", _SAFE_REFERENCE)
        _require_positive_int(self.fencing_generation, "endpoint_fencing_generation_invalid")


@dataclass(frozen=True)
class ComputeResult:
    """A typed, body-free result envelope; raw result validation stays in its adapter."""

    lease_id: str
    endpoint_id: str
    fencing_generation: int
    capability: str
    contract_version: str
    identity: EndpointIdentityEvidence
    result_schema: str
    result_digest: str
    result_item_count: int
    vector_dimension: int | None = None
    capability_binding_fingerprint: str | None = None
    terminal_reason: str = "completed"

    def __post_init__(self) -> None:
        _require_identifier(self.lease_id, "endpoint_compute_lease_id_invalid", _SAFE_REFERENCE)
        _require_identifier(self.endpoint_id, "endpoint_id_invalid", _ENDPOINT_ID)
        _require_positive_int(self.fencing_generation, "endpoint_fencing_generation_invalid")
        _require_capability_contract(
            self.capability,
            self.contract_version,
            capability_code="endpoint_capability_schema_unsupported",
        )
        _require_result_schema(self.capability, self.result_schema)
        _require_sha256(self.result_digest, "endpoint_result_digest_invalid")
        _require_nonnegative_int(self.result_item_count, "endpoint_result_item_count_invalid")
        if self.capability == "embedding":
            if self.vector_dimension is None:
                raise EndpointContractError("endpoint_result_vector_dimension_required")
            _require_positive_int(self.vector_dimension, "endpoint_result_vector_dimension_invalid")
        elif self.vector_dimension is not None:
            raise EndpointContractError("endpoint_result_vector_dimension_unexpected")
        if self.capability_binding_fingerprint is not None:
            _require_sha256(
                self.capability_binding_fingerprint,
                "endpoint_capability_binding_fingerprint_invalid",
            )
        _require_terminal_reason(self.terminal_reason, "endpoint_result_terminal_reason_invalid")


@dataclass(frozen=True)
class ComputeCompletionDecision:
    """Completion decision for an existing durable-work/fencing adapter."""

    accepted: bool
    retryable: bool
    reason_code: str
    receipt: DeploymentReceipt
    quarantine_recommended: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.reason_code, "endpoint_completion_reason_invalid", _SAFE_CODE)
        if self.receipt.outcome != ("accepted" if self.accepted else "rejected"):
            raise EndpointContractError("endpoint_completion_receipt_invalid")


class EndpointAuthority:
    """The three-operation deep module for endpoint topology and compute admission."""

    def resolve(self, manifest: EndpointManifestV2) -> ResolvedEndpointDeploymentPlan:
        """Resolve static role ownership without creating state or executing work."""

        backend = next(item for item in manifest.endpoints if item.role == PP_SERVER_BACKEND)
        return ResolvedEndpointDeploymentPlan(
            manifest=manifest,
            manifest_digest=manifest.manifest_digest,
            canonical_sqlite_owner=backend.endpoint_id,
            lancedb_promotion_owner=backend.endpoint_id,
            receipt_persistence_owner=backend.endpoint_id,
        )

    def assess(
        self,
        plan: ResolvedEndpointDeploymentPlan,
        observation: EndpointObservation,
        requirement: EndpointRequirement,
        *,
        observed_at: datetime,
    ) -> EndpointAdmission:
        """Assess a private observation without leaking transport or provider details."""

        observed_at = _require_utc(observed_at, "endpoint_observation_timestamp_invalid")
        declaration = plan.endpoint_by_id(observation.hello.endpoint_id)
        if declaration is None:
            return self._reject_admission(plan, observation, observed_at, "endpoint_not_declared")
        if declaration.role != PP_COMPUTE_NODE or observation.hello.role != PP_COMPUTE_NODE:
            return self._reject_admission(
                plan,
                observation,
                observed_at,
                "endpoint_role_assignment_invalid",
                quarantine=True,
            )
        if not self._protocol_compatible(declaration.protocol, observation.hello.protocol):
            return self._reject_admission(
                plan,
                observation,
                observed_at,
                "endpoint_protocol_major_incompatible",
                quarantine=True,
            )
        if observation.hello.protocol.minor < declaration.protocol.minor:
            return self._reject_admission(
                plan,
                observation,
                observed_at,
                "endpoint_protocol_minor_unsupported",
                quarantine=True,
            )
        if not self._protocol_compatible(requirement.protocol, observation.hello.protocol):
            return self._reject_admission(
                plan, observation, observed_at, "endpoint_protocol_major_incompatible"
            )
        if observation.hello.protocol.minor < requirement.protocol.minor:
            return self._reject_admission(
                plan,
                observation,
                observed_at,
                "endpoint_protocol_minor_unsupported",
                quarantine=True,
            )
        declared_capability = declaration.capability_for(
            requirement.capability,
            requirement.contract_version,
        )
        if declared_capability is None:
            return self._reject_admission(
                plan, observation, observed_at, "endpoint_capability_missing"
            )
        observed_capability = observation.hello.capability_for(
            requirement.capability,
            requirement.contract_version,
        )
        if observed_capability is None:
            return self._reject_admission(
                plan,
                observation,
                observed_at,
                "endpoint_capability_missing",
                quarantine=True,
            )
        declared_binding = declared_capability.binding
        if declared_binding is None:
            if requirement.capability_binding is not None:
                return self._reject_admission(
                    plan,
                    observation,
                    observed_at,
                    "endpoint_capability_binding_not_declared",
                    quarantine=True,
                )
        else:
            if (
                observed_capability.binding is None
                or observed_capability.binding.fingerprint != declared_binding.fingerprint
            ):
                return self._reject_admission(
                    plan,
                    observation,
                    observed_at,
                    "endpoint_capability_binding_incompatible",
                    quarantine=True,
                )
            if (
                requirement.capability_binding is not None
                and requirement.capability_binding.fingerprint != declared_binding.fingerprint
            ):
                return self._reject_admission(
                    plan,
                    observation,
                    observed_at,
                    "endpoint_capability_binding_incompatible",
                    quarantine=True,
                )
        if (
            requirement.allowed_endpoint_ids
            and declaration.endpoint_id not in requirement.allowed_endpoint_ids
        ):
            return self._reject_admission(plan, observation, observed_at, "endpoint_not_allowed")
        if (
            requirement.pinned_endpoint_id is not None
            and declaration.endpoint_id != requirement.pinned_endpoint_id
        ):
            return self._reject_admission(plan, observation, observed_at, "endpoint_not_pinned")
        if not observation.heartbeat.is_fresh(observed_at):
            return self._reject_admission(
                plan,
                observation,
                observed_at,
                "endpoint_heartbeat_stale",
                retryable=True,
            )
        assert observation.resources is not None
        if observation.resources.max_concurrency != declaration.max_concurrency:
            return self._reject_admission(
                plan,
                observation,
                observed_at,
                "endpoint_resource_report_invalid",
                quarantine=True,
            )
        if observation.resources.available_slots < requirement.minimum_available_slots:
            return self._reject_admission(
                plan,
                observation,
                observed_at,
                "endpoint_capacity_exhausted",
                retryable=True,
            )
        actual_identity = observation.hello.identity
        assert actual_identity is not None
        expected_fingerprint = requirement.required_identity.fingerprint_for(requirement.capability)
        actual_fingerprint = actual_identity.fingerprint_for(requirement.capability)
        if actual_fingerprint is None or actual_fingerprint != expected_fingerprint:
            return self._reject_admission(
                plan,
                observation,
                observed_at,
                "endpoint_embedding_identity_incompatible"
                if requirement.capability == "embedding"
                else "endpoint_rerank_identity_incompatible",
                quarantine=True,
            )
        if declared_binding is not None and (
            actual_fingerprint != declared_binding.model_identity_fingerprint
            or expected_fingerprint != declared_binding.model_identity_fingerprint
        ):
            return self._reject_admission(
                plan,
                observation,
                observed_at,
                "endpoint_capability_identity_binding_incompatible",
                quarantine=True,
            )
        if declared_binding is not None and (
            observation.resources.memory_free_mib < declared_binding.resources.minimum_memory_mib
            or observation.resources.model_cache_free_bytes
            < declared_binding.resources.minimum_model_cache_bytes
        ):
            return self._reject_admission(
                plan,
                observation,
                observed_at,
                "endpoint_capability_resource_insufficient",
                retryable=True,
            )
        binding = EndpointBinding(
            manifest_digest=plan.manifest_digest,
            endpoint_id=declaration.endpoint_id,
            capability=requirement.capability,
            contract_version=requirement.contract_version,
            protocol=observation.hello.protocol,
            identity_fingerprint=actual_fingerprint,
            heartbeat_sequence=observation.heartbeat.sequence,
            resource_report_generation=observation.resources.report_generation,
            bound_at=observed_at,
            capability_binding=declared_binding,
        )
        receipt = self._receipt(
            plan=plan,
            endpoint_id=declaration.endpoint_id,
            event="endpoint-admitted",
            outcome="accepted",
            reason_code="endpoint_admitted",
            observed_at=observed_at,
        )
        return EndpointAdmission(
            accepted=True,
            reason_code="endpoint_admitted",
            receipt=receipt,
            binding=binding,
        )

    def verify_completion(
        self,
        binding: EndpointBinding,
        lease: ComputeLease,
        result: ComputeResult,
        current_fence: ComputeFence,
        *,
        observed_at: datetime,
    ) -> ComputeCompletionDecision:
        """Validate a derived result before the durable-work store completes its lease."""

        observed_at = _require_utc(observed_at, "endpoint_observation_timestamp_invalid")
        reason = self._completion_mismatch(binding, lease, result, current_fence, observed_at)
        if reason is not None:
            return self._reject_completion(
                binding,
                current_fence,
                observed_at,
                reason,
                retryable=reason
                in {
                    "endpoint_compute_lease_expired",
                    "endpoint_compute_fencing_stale",
                },
                quarantine=reason
                in {
                    "endpoint_capability_binding_mismatch",
                    "endpoint_result_identity_drift",
                    "endpoint_result_payload_invalid",
                    "endpoint_result_terminal_reason_invalid",
                    "endpoint_contract_digest_mismatch",
                },
            )
        receipt = self._receipt(
            manifest_digest=binding.manifest_digest,
            endpoint_id=binding.endpoint_id,
            event="compute-completed",
            outcome="accepted",
            reason_code="endpoint_compute_completed",
            observed_at=observed_at,
            fencing_generation=current_fence.fencing_generation,
        )
        return ComputeCompletionDecision(
            accepted=True,
            retryable=False,
            reason_code="endpoint_compute_completed",
            receipt=receipt,
        )

    @staticmethod
    def _protocol_compatible(expected: EndpointProtocol, observed: EndpointProtocol) -> bool:
        return expected.family == observed.family and expected.major == observed.major

    def _completion_mismatch(
        self,
        binding: EndpointBinding,
        lease: ComputeLease,
        result: ComputeResult,
        current_fence: ComputeFence,
        observed_at: datetime,
    ) -> str | None:
        if lease.manifest_digest != binding.manifest_digest:
            return "endpoint_contract_digest_mismatch"
        if lease.endpoint_id != binding.endpoint_id or result.endpoint_id != binding.endpoint_id:
            return "endpoint_result_endpoint_mismatch"
        if result.lease_id != lease.lease_id:
            return "endpoint_result_lease_mismatch"
        if current_fence.job_id != lease.job_id:
            return "endpoint_compute_fence_scope_invalid"
        if lease.fencing_generation != current_fence.fencing_generation:
            return "endpoint_compute_fencing_stale"
        if result.fencing_generation != lease.fencing_generation:
            return "endpoint_compute_fencing_stale"
        if observed_at > _require_utc(lease.expires_at, "endpoint_lease_timestamp_invalid"):
            return "endpoint_compute_lease_expired"
        if (
            lease.capability != binding.capability
            or result.capability != binding.capability
            or lease.contract_version != binding.contract_version
            or result.contract_version != binding.contract_version
            or lease.result_schema != result.result_schema
        ):
            return "endpoint_result_payload_invalid"
        capability_binding = binding.capability_binding
        if capability_binding is not None:
            binding_fingerprint = capability_binding.fingerprint
            if (
                lease.capability_binding_fingerprint != binding_fingerprint
                or result.capability_binding_fingerprint != binding_fingerprint
                or lease.input_schema != capability_binding.input_schema
                or lease.result_schema != capability_binding.result_schema
                or lease.required_identity_fingerprint
                != capability_binding.model_identity_fingerprint
            ):
                return "endpoint_capability_binding_mismatch"
            lease_duration = _require_utc(
                lease.expires_at,
                "endpoint_lease_timestamp_invalid",
            ) - _require_utc(lease.issued_at, "endpoint_lease_timestamp_invalid")
            if lease_duration > timedelta(seconds=capability_binding.lease.timeout_seconds):
                return "endpoint_capability_lease_timeout_exceeded"
            if result.terminal_reason not in capability_binding.lease.terminal_reasons:
                return "endpoint_result_terminal_reason_invalid"
            if (
                result.terminal_reason == "cancelled"
                and not capability_binding.lease.cancel_supported
            ):
                return "endpoint_result_terminal_reason_invalid"
        actual_fingerprint = result.identity.fingerprint_for(result.capability)
        if (
            actual_fingerprint is None
            or actual_fingerprint != binding.identity_fingerprint
            or actual_fingerprint != lease.required_identity_fingerprint
        ):
            return "endpoint_result_identity_drift"
        if result.capability == "embedding":
            expected_dimension = (
                result.identity.embedding.dimension if result.identity.embedding else None
            )
            if result.vector_dimension != expected_dimension:
                return "endpoint_result_payload_invalid"
        return None

    def _reject_admission(
        self,
        plan: ResolvedEndpointDeploymentPlan,
        observation: EndpointObservation,
        observed_at: datetime,
        reason_code: str,
        *,
        retryable: bool = False,
        quarantine: bool = False,
    ) -> EndpointAdmission:
        return EndpointAdmission(
            accepted=False,
            reason_code=reason_code,
            receipt=self._receipt(
                plan=plan,
                endpoint_id=observation.hello.endpoint_id,
                event="endpoint-admitted",
                outcome="rejected",
                reason_code=reason_code,
                observed_at=observed_at,
            ),
            retryable=retryable,
            quarantine_recommended=quarantine,
        )

    def _reject_completion(
        self,
        binding: EndpointBinding,
        current_fence: ComputeFence,
        observed_at: datetime,
        reason_code: str,
        *,
        retryable: bool,
        quarantine: bool,
    ) -> ComputeCompletionDecision:
        return ComputeCompletionDecision(
            accepted=False,
            retryable=retryable,
            reason_code=reason_code,
            receipt=self._receipt(
                manifest_digest=binding.manifest_digest,
                endpoint_id=binding.endpoint_id,
                event="compute-completed",
                outcome="rejected",
                reason_code=reason_code,
                observed_at=observed_at,
                fencing_generation=current_fence.fencing_generation,
            ),
            quarantine_recommended=quarantine,
        )

    @staticmethod
    def _receipt(
        *,
        event: str,
        outcome: str,
        reason_code: str,
        observed_at: datetime,
        plan: ResolvedEndpointDeploymentPlan | None = None,
        manifest_digest: str | None = None,
        endpoint_id: str | None,
        fencing_generation: int | None = None,
    ) -> DeploymentReceipt:
        if plan is not None:
            manifest_digest = plan.manifest_digest
        assert manifest_digest is not None
        payload = {
            "manifest_digest": manifest_digest,
            "endpoint_id": endpoint_id,
            "event": event,
            "outcome": outcome,
            "reason_code": reason_code,
            "observed_at": _utc_json(observed_at),
            "fencing_generation": fencing_generation,
        }
        receipt_id = f"r-{hashlib.sha256(_canonical_json(payload)).hexdigest()[:24]}"
        return DeploymentReceipt(
            receipt_id=receipt_id,
            manifest_digest=manifest_digest,
            endpoint_id=endpoint_id,
            event=event,
            outcome=outcome,
            reason_code=reason_code,
            observed_at=observed_at,
            fencing_generation=fencing_generation,
        )


# Backwards-compatible name for callers that adopted the original contract.
EndpointContractRegistry = EndpointAuthority


def _parse_protocol(value: object) -> EndpointProtocol:
    payload = _require_mapping(value, "endpoint_protocol_mapping_required")
    unknown = sorted(set(payload) - _PROTOCOL_FIELDS)
    if unknown:
        raise EndpointContractError(f"endpoint_protocol_unknown_field:{unknown[0]}")
    if set(payload) != _PROTOCOL_FIELDS:
        raise EndpointContractError("endpoint_protocol_field_required")
    return EndpointProtocol(
        family=_safe_model_field(payload["family"], "endpoint_protocol_family_invalid"),
        major=_require_positive_int(payload["major"], "endpoint_protocol_major_invalid"),
        minor=_require_nonnegative_int(payload["minor"], "endpoint_protocol_minor_invalid"),
    )


def _parse_capability_binding(capability: str, value: object) -> CapabilityBinding:
    payload = _require_mapping(value, "endpoint_capability_binding_mapping_required")
    unknown = sorted(set(payload) - _CAPABILITY_BINDING_FIELDS)
    if unknown:
        raise EndpointContractError(f"endpoint_capability_binding_unknown_field:{unknown[0]}")
    if set(payload) != _CAPABILITY_BINDING_FIELDS:
        raise EndpointContractError("endpoint_capability_binding_field_required")
    resources = _require_mapping(
        payload["resources"],
        "endpoint_capability_resources_mapping_required",
    )
    resource_unknown = sorted(set(resources) - _CAPABILITY_RESOURCE_FIELDS)
    if resource_unknown:
        raise EndpointContractError(
            f"endpoint_capability_resources_unknown_field:{resource_unknown[0]}"
        )
    if set(resources) != _CAPABILITY_RESOURCE_FIELDS:
        raise EndpointContractError("endpoint_capability_resources_field_required")
    lease = _require_mapping(payload["lease"], "endpoint_capability_lease_mapping_required")
    lease_unknown = sorted(set(lease) - _CAPABILITY_LEASE_FIELDS)
    if lease_unknown:
        raise EndpointContractError(f"endpoint_capability_lease_unknown_field:{lease_unknown[0]}")
    if set(lease) != _CAPABILITY_LEASE_FIELDS:
        raise EndpointContractError("endpoint_capability_lease_field_required")
    terminal_reasons_value = lease["terminal_reasons"]
    if not isinstance(terminal_reasons_value, list):
        raise EndpointContractError("endpoint_capability_terminal_reasons_list_required")
    golden_probe = _require_mapping(
        payload["golden_probe"],
        "endpoint_golden_probe_mapping_required",
    )
    golden_probe_unknown = sorted(set(golden_probe) - _GOLDEN_PROBE_FIELDS)
    if golden_probe_unknown:
        raise EndpointContractError(
            f"endpoint_golden_probe_unknown_field:{golden_probe_unknown[0]}"
        )
    if set(golden_probe) != _GOLDEN_PROBE_FIELDS:
        raise EndpointContractError("endpoint_golden_probe_field_required")
    binding = CapabilityBinding(
        model_identity_fingerprint=_require_sha256(
            payload["model_identity_fingerprint"],
            "endpoint_capability_identity_fingerprint_invalid",
        ),
        input_schema=_require_string(payload["input_schema"], "endpoint_input_schema_invalid"),
        result_schema=_require_string(payload["result_schema"], "endpoint_result_schema_invalid"),
        resources=CapabilityResourceBinding(
            minimum_memory_mib=_require_nonnegative_int(
                resources["minimum_memory_mib"],
                "endpoint_capability_minimum_memory_mib_invalid",
            ),
            minimum_model_cache_bytes=_require_nonnegative_int(
                resources["minimum_model_cache_bytes"],
                "endpoint_capability_minimum_model_cache_bytes_invalid",
            ),
        ),
        max_concurrency=_require_positive_int(
            payload["max_concurrency"],
            "endpoint_capability_max_concurrency_invalid",
        ),
        lease=CapabilityLeaseBinding(
            timeout_seconds=_require_positive_int(
                lease["timeout_seconds"],
                "endpoint_capability_timeout_seconds_invalid",
            ),
            idempotency_key_schema=_require_string(
                lease["idempotency_key_schema"],
                "endpoint_capability_idempotency_schema_unsupported",
            ),
            cancel_supported=lease["cancel_supported"],
            terminal_reasons=tuple(terminal_reasons_value),
        ),
        golden_probe=GoldenProbeBinding(
            input_schema=_require_string(
                golden_probe["input_schema"],
                "endpoint_golden_probe_input_schema_invalid",
            ),
            result_schema=_require_string(
                golden_probe["result_schema"],
                "endpoint_golden_probe_result_schema_invalid",
            ),
            probe_input_sha256=_require_sha256(
                golden_probe["probe_input_sha256"],
                "endpoint_golden_probe_input_sha256_invalid",
            ),
            expected_result_sha256=_require_sha256(
                golden_probe["expected_result_sha256"],
                "endpoint_golden_probe_result_sha256_invalid",
            ),
        ),
    )
    binding.validate_for(capability)
    return binding


def _parse_capabilities(value: object) -> tuple[EndpointCapability, ...]:
    if not isinstance(value, list):
        raise EndpointContractError("endpoint_capabilities_list_required")
    parsed: list[EndpointCapability] = []
    for raw in value:
        payload = _require_mapping(raw, "endpoint_capability_mapping_required")
        unknown = sorted(set(payload) - _CAPABILITY_FIELDS)
        if unknown:
            raise EndpointContractError(f"endpoint_capability_unknown_field:{unknown[0]}")
        required = {"kind", "contract_version"}
        if not required.issubset(payload):
            raise EndpointContractError("endpoint_capability_field_required")
        capability, contract_version = _require_capability_contract(
            payload["kind"],
            payload["contract_version"],
            capability_code="endpoint_capability_invalid",
        )
        parsed.append(
            EndpointCapability(
                kind=capability,
                contract_version=contract_version,
                binding=(
                    _parse_capability_binding(capability, payload["binding"])
                    if "binding" in payload
                    else None
                ),
            )
        )
    return tuple(parsed)


def _parse_endpoint(value: object) -> EndpointDeclaration:
    payload = _require_mapping(value, "endpoint_mapping_required")
    unknown = sorted(set(payload) - _ENDPOINT_FIELDS)
    if unknown:
        raise EndpointContractError(f"endpoint_unknown_field:{unknown[0]}")
    required = {"id", "role", "protocol", "capabilities", "transport_ref", "resource_policy_ref"}
    if not required.issubset(payload):
        raise EndpointContractError("endpoint_field_required")
    role = _require_string(payload["role"], "endpoint_role_invalid")
    max_concurrency = payload.get("max_concurrency")
    if role == PP_COMPUTE_NODE and max_concurrency is None:
        raise EndpointContractError("endpoint_compute_max_concurrency_required")
    return EndpointDeclaration(
        endpoint_id=_require_identifier(payload["id"], "endpoint_id_invalid", _ENDPOINT_ID),
        role=role,
        protocol=_parse_protocol(payload["protocol"]),
        capabilities=_parse_capabilities(payload["capabilities"]),
        transport_ref=_require_identifier(
            payload["transport_ref"], "endpoint_transport_reference_invalid", _SAFE_REFERENCE
        ),
        resource_policy_ref=_require_identifier(
            payload["resource_policy_ref"],
            "endpoint_resource_policy_reference_invalid",
            _SAFE_REFERENCE,
        ),
        max_concurrency=(
            _require_positive_int(max_concurrency, "endpoint_compute_max_concurrency_invalid")
            if max_concurrency is not None
            else None
        ),
    )


def _resolve_module_ids(profile_id: str, value: object) -> tuple[str, ...]:
    profile = profile_by_id(profile_id)
    if profile is None:
        raise EndpointContractError("endpoint_profile_unsupported")
    selections = _require_mapping(value, "endpoint_modules_mapping_required")
    selected = list(profile.base_modules)
    for module_id in selections:
        if not isinstance(module_id, str):
            raise EndpointContractError("endpoint_module_id_invalid")
        if not any(module.id == module_id for module in deployment_modules()):
            raise EndpointContractError("endpoint_module_unsupported")
    for module in deployment_modules():
        if module.id not in selections:
            continue
        if module.risk_tier == "core":
            raise EndpointContractError("endpoint_core_module_implicit")
        if profile_id not in module.supported_profiles:
            raise EndpointContractError("endpoint_module_profile_incompatible")
        selection = _require_mapping(
            selections[module.id], "endpoint_module_selection_mapping_required"
        )
        unknown = sorted(set(selection) - _MODULE_SELECTION_FIELDS)
        if unknown:
            raise EndpointContractError(f"endpoint_module_unknown_field:{module.id}:{unknown[0]}")
        if selection.get("enabled") is not True:
            raise EndpointContractError("endpoint_module_enabled_must_be_true")
        if module.risk_tier == "high-risk" and selection.get("acknowledge_high_risk") is not True:
            raise EndpointContractError("endpoint_high_risk_module_acknowledgement_required")
        if module.risk_tier != "high-risk" and "acknowledge_high_risk" in selection:
            raise EndpointContractError("endpoint_high_risk_acknowledgement_unexpected")
        for required in module.requires:
            if required not in selected:
                selected.append(required)
        if module.id not in selected:
            selected.append(module.id)
    return tuple(selected)


def _parse_resource_budget(value: object | None) -> ResourceBudgetEvidence | None:
    if value is None:
        return None
    payload = _require_mapping(value, "endpoint_resource_budget_mapping_required")
    unknown = sorted(set(payload) - _RESOURCE_BUDGET_FIELDS)
    if unknown:
        raise EndpointContractError(f"endpoint_resource_budget_unknown_field:{unknown[0]}")
    if set(payload) != _RESOURCE_BUDGET_FIELDS:
        raise EndpointContractError("endpoint_resource_budget_field_required")
    return ResourceBudgetEvidence(
        **{
            field: _require_nonnegative_int(
                payload[field], f"endpoint_resource_budget_invalid:{field}"
            )
            for field in _RESOURCE_BUDGET_FIELDS
        }
    )


def _parse_resource_locations(value: object | None) -> ResourceLocationReferences | None:
    if value is None:
        return None
    payload = _require_mapping(value, "endpoint_resource_locations_mapping_required")
    unknown = sorted(set(payload) - _RESOURCE_LOCATION_FIELDS)
    if unknown:
        raise EndpointContractError(f"endpoint_resource_locations_unknown_field:{unknown[0]}")
    values: dict[str, str | None] = {}
    for field in _RESOURCE_LOCATION_FIELDS:
        raw = payload.get(field)
        values[field] = (
            None
            if raw is None
            else _require_identifier(
                raw, f"endpoint_resource_location_invalid:{field}", _SAFE_REFERENCE
            )
        )
    return ResourceLocationReferences(**values)


def parse_deployment_manifest_v2(payload: Mapping[str, object]) -> EndpointManifestV2:
    """Parse a V2 topology contract without accepting paths, endpoints, or secrets."""

    manifest = _require_mapping(payload, "endpoint_manifest_mapping_required")
    _scan_for_secrets(manifest)
    unknown = sorted(set(manifest) - _MANIFEST_FIELDS)
    if unknown:
        raise EndpointContractError(f"endpoint_manifest_unknown_field:{unknown[0]}")
    required = {"schema_version", "deployment_id", "profile", "modules", "endpoints"}
    if not required.issubset(manifest):
        raise EndpointContractError("endpoint_manifest_field_required")
    if manifest["schema_version"] != DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION:
        raise EndpointContractError("endpoint_manifest_schema_unsupported")
    profile_id = _require_string(manifest["profile"], "endpoint_profile_unsupported")
    endpoints_value = manifest["endpoints"]
    if not isinstance(endpoints_value, list):
        raise EndpointContractError("endpoint_manifest_endpoints_list_required")
    return EndpointManifestV2(
        deployment_id=_require_identifier(
            manifest["deployment_id"], "endpoint_deployment_id_invalid", _DEPLOYMENT_ID
        ),
        profile_id=profile_id,
        module_ids=_resolve_module_ids(profile_id, manifest["modules"]),
        endpoints=tuple(_parse_endpoint(item) for item in endpoints_value),
        resource_budget=_parse_resource_budget(manifest.get("resource_budget")),
        resource_locations=_parse_resource_locations(manifest.get("resource_locations")),
    )


def resolve_deployment_manifest_v2(payload: Mapping[str, object]) -> ResolvedEndpointDeploymentPlan:
    """Resolve a parsed V2 manifest through the public deep-module seam."""

    return EndpointAuthority().resolve(parse_deployment_manifest_v2(payload))


def admit_endpoint(
    plan: ResolvedEndpointDeploymentPlan,
    observation: EndpointObservation,
    requirement: EndpointRequirement,
    *,
    observed_at: datetime,
) -> EndpointAdmission:
    """Convenience function for the second public contract operation."""

    return EndpointAuthority().assess(plan, observation, requirement, observed_at=observed_at)


def validate_compute_exchange(
    binding: EndpointBinding,
    lease: ComputeLease,
    result: ComputeResult,
    current_fence: ComputeFence,
    *,
    observed_at: datetime,
) -> ComputeCompletionDecision:
    """Convenience function for typed completion validation."""

    return EndpointAuthority().verify_completion(
        binding,
        lease,
        result,
        current_fence,
        observed_at=observed_at,
    )


__all__ = [
    "DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION",
    "DEPLOYMENT_RECEIPT_SCHEMA_VERSION",
    "ENDPOINT_CONTRACT_SCHEMA_VERSION",
    "MANIFEST_REVISION_SCHEMA_VERSION",
    "PP_COMPUTE_NODE",
    "PP_LOCAL_EDGE",
    "PP_SERVER_BACKEND",
    "AcceleratorResource",
    "CapabilityBinding",
    "CapabilityLeaseBinding",
    "CapabilityResourceBinding",
    "ComputeCompletionDecision",
    "ComputeFence",
    "ComputeLease",
    "ComputeResult",
    "DeploymentReceipt",
    "EmbeddingIdentity",
    "EndpointAdmission",
    "EndpointAuthority",
    "EndpointAuthorityProfile",
    "EndpointBinding",
    "EndpointCapability",
    "EndpointContractError",
    "EndpointContractRegistry",
    "EndpointDeclaration",
    "EndpointHeartbeat",
    "EndpointHello",
    "EndpointIdentityEvidence",
    "EndpointManifestV2",
    "EndpointObservation",
    "EndpointProtocol",
    "EndpointRequirement",
    "EndpointResourceReport",
    "GoldenProbeBinding",
    "ManifestRevisionRecord",
    "ResolvedEndpointDeploymentPlan",
    "ResourceBudgetEvidence",
    "ResourceLocationReferences",
    "RerankIdentity",
    "admit_endpoint",
    "parse_deployment_manifest_v2",
    "resolve_deployment_manifest_v2",
    "validate_compute_exchange",
]
