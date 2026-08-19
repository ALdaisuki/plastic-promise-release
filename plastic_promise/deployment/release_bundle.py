"""Pure, secret-free contracts for model-catalog-bound release bundles.

This module is deliberately a local verification seam.  CI, a release builder,
or a future attestation adapter may verify signatures elsewhere and pass only a
small, already-verified evidence projection here.  It never downloads model
weights, opens a registry, reads credentials, or performs cryptography.

``ModelCatalog`` captures the complete model identity that makes vectors and
rerank scores interoperable.  ``ReleaseBundle`` binds that catalog to a
specific source revision, release-manifest hash, inspected OCI artifact bundle,
and optional rollback identity.  The resulting canonical digests are stable
across JSON key ordering and evidence input ordering.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from plastic_promise.endpoint_roles import compute_package_manifest

from ..release_manifest import package_version_for_release, release_channel
from .catalog import profile_by_id
from .container_artifacts import CONTAINER_ARTIFACT_BUNDLE_SCHEMA_VERSION
from .endpoint_contract import (
    PP_COMPUTE_NODE,
    PP_LOCAL_EDGE,
    PP_SERVER_BACKEND,
    EmbeddingIdentity,
    EndpointCapability,
    RerankIdentity,
)

if TYPE_CHECKING:
    from .container_artifacts import ArtifactBundle


MODEL_CATALOG_SCHEMA_VERSION = "plastic-promise-model-catalog/v1"
ARTIFACT_SBOM_RECEIPTS_SCHEMA_VERSION = "plastic-promise-artifact-sbom-receipts/v1"
ARTIFACT_BUNDLE_BINDING_SCHEMA_VERSION = "plastic-promise-artifact-binding/v2"
RELEASE_EVIDENCE_SCHEMA_VERSION = "plastic-promise-release-evidence/v1"
RELEASE_BUNDLE_SCHEMA_VERSION = "plastic-promise-release-bundle/v1"

MODEL_RUNTIME_LOCAL = "local-inference"
MODEL_RUNTIME_CLOUD = "cloud-inference"
MODEL_RUNTIME_REMOTE = "remote-inference"

_RUNTIME_PROFILE_COMPATIBILITY = {
    MODEL_RUNTIME_LOCAL: frozenset({"local-all-in-one"}),
    MODEL_RUNTIME_CLOUD: frozenset({"local-all-in-one", "local-cloud"}),
    MODEL_RUNTIME_REMOTE: frozenset({"split-accelerated"}),
}
_EVIDENCE_SUBJECTS = frozenset(
    {"release-manifest", "model-catalog", "artifact-bundle", "artifact-sbom-receipts"}
)
_ARTIFACT_ROLES = frozenset({PP_LOCAL_EDGE, PP_SERVER_BACKEND, PP_COMPUTE_NODE})
_SUPPORTED_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})
_COMPUTE_VARIANTS = frozenset({"cpu", "cuda"})
_COMPUTE_PACKAGE_MANIFEST = compute_package_manifest()
_COMPUTE_CAPABILITY_KINDS = frozenset(
    capability.kind for capability in _COMPUTE_PACKAGE_MANIFEST.capabilities
)
_COMPUTE_CAPABILITY_CONTRACTS = frozenset(_COMPUTE_PACKAGE_MANIFEST.capability_contracts)
_CATALOG_REFERENCE = re.compile(r"^[a-z][a-z0-9._-]{1,127}$")
_SAFE_REFERENCE = re.compile(r"^[a-z][a-z0-9._-]{1,127}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_:-]{1,127}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RAW_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SBOM_SPEC_VERSION = re.compile(r"^[1-9][0-9]*\.[0-9]+(?:\.[0-9]+)?$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9-]{1,63}/v[1-9][0-9]*$")
_PACKAGE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MAX_RESOURCE_BYTES = (2**63) - 1
_SECRET_FIELD_TOKENS = frozenset(
    {"apikey", "authorization", "credential", "password", "privatekey", "secret", "token"}
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class ReleaseBundleError(ValueError):
    """A stable, sanitised error at the model-catalog release seam."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("release_bundle_error_code_invalid")
        self.code = code
        super().__init__(code)

    def public_json(self) -> dict[str, str]:
        """Return the only error data safe to expose to a UI or receipt."""

        return {"code": self.code}


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _digest(payload: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


def _require_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReleaseBundleError(code)
    return value


def _require_raw_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or _RAW_SHA256.fullmatch(value) is None:
        raise ReleaseBundleError(code)
    return value


def _require_source_sha(value: object, code: str) -> str:
    if not isinstance(value, str) or _SOURCE_SHA.fullmatch(value) is None:
        raise ReleaseBundleError(code)
    return value


def _require_reference(value: object, code: str) -> str:
    if not isinstance(value, str) or _SAFE_REFERENCE.fullmatch(value) is None:
        raise ReleaseBundleError(code)
    if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise ReleaseBundleError("release_bundle_secret_forbidden")
    return value


def _require_catalog_reference(value: object, code: str) -> str:
    if not isinstance(value, str) or _CATALOG_REFERENCE.fullmatch(value) is None:
        raise ReleaseBundleError(code)
    if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise ReleaseBundleError("release_bundle_secret_forbidden")
    return value


def _require_package_version(value: object, code: str) -> str:
    if not isinstance(value, str) or _PACKAGE_VERSION.fullmatch(value) is None:
        raise ReleaseBundleError(code)
    if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise ReleaseBundleError("release_bundle_secret_forbidden")
    return value


def _require_release_version(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise ReleaseBundleError(code)
    try:
        package_version_for_release(value)
    except ValueError as exc:
        raise ReleaseBundleError(code) from exc
    return value


def _require_mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ReleaseBundleError(code)
    return value


def _require_sequence(value: object, code: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ReleaseBundleError(code)
    return value


def _reject_secrets(value: object) -> None:
    """Fail closed before deserialising arbitrary operator-supplied JSON."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ReleaseBundleError("release_bundle_fields_invalid")
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in _SECRET_FIELD_TOKENS:
                raise ReleaseBundleError("release_bundle_secret_forbidden")
            _reject_secrets(nested)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            _reject_secrets(nested)
        return
    if isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise ReleaseBundleError("release_bundle_secret_forbidden")


def _require_fields(
    payload: Mapping[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    code: str,
) -> None:
    if not required.issubset(payload) or set(payload) - required - optional:
        raise ReleaseBundleError(code)


def _require_resource_bytes(value: object, code: str, *, allow_zero: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReleaseBundleError(code)
    minimum = 0 if allow_zero else 1
    if not minimum <= value <= _MAX_RESOURCE_BYTES:
        raise ReleaseBundleError(code)
    return value


def _capability_contracts(capabilities: tuple[EndpointCapability, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            f"{capability.kind}/v{capability.contract_version.rsplit('v', 1)[1]}"
            for capability in capabilities
        )
    )


def _identity_model_is_safe(model: str) -> bool:
    """Permit namespace/model identifiers while rejecting local paths and URLs."""

    return not (
        model.startswith(("/", ".", "~"))
        or "\\" in model
        or ".." in model
        or "://" in model
        or any(pattern.search(model) for pattern in _SECRET_VALUE_PATTERNS)
    )


@dataclass(frozen=True)
class ModelResourceEstimate:
    """Non-secret minimum resources needed to materialise and run a model set."""

    model_cache_bytes: int
    minimum_system_memory_bytes: int
    minimum_gpu_memory_bytes: int

    def __post_init__(self) -> None:
        _require_resource_bytes(
            self.model_cache_bytes,
            "model_catalog_resource_model_cache_bytes_invalid",
            allow_zero=False,
        )
        _require_resource_bytes(
            self.minimum_system_memory_bytes,
            "model_catalog_resource_system_memory_bytes_invalid",
            allow_zero=False,
        )
        _require_resource_bytes(
            self.minimum_gpu_memory_bytes,
            "model_catalog_resource_gpu_memory_bytes_invalid",
            allow_zero=True,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "model_cache_bytes": self.model_cache_bytes,
            "minimum_system_memory_bytes": self.minimum_system_memory_bytes,
            "minimum_gpu_memory_bytes": self.minimum_gpu_memory_bytes,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ModelResourceEstimate:
        mapping = _require_mapping(payload, "model_catalog_resource_invalid")
        _reject_secrets(mapping)
        _require_fields(
            mapping,
            required=frozenset(
                {"model_cache_bytes", "minimum_system_memory_bytes", "minimum_gpu_memory_bytes"}
            ),
            code="model_catalog_resource_fields_invalid",
        )
        return cls(
            model_cache_bytes=_require_resource_bytes(
                mapping["model_cache_bytes"],
                "model_catalog_resource_model_cache_bytes_invalid",
                allow_zero=False,
            ),
            minimum_system_memory_bytes=_require_resource_bytes(
                mapping["minimum_system_memory_bytes"],
                "model_catalog_resource_system_memory_bytes_invalid",
                allow_zero=False,
            ),
            minimum_gpu_memory_bytes=_require_resource_bytes(
                mapping["minimum_gpu_memory_bytes"],
                "model_catalog_resource_gpu_memory_bytes_invalid",
                allow_zero=True,
            ),
        )


@dataclass(frozen=True)
class ModelCatalog:
    """Versioned, complete identity metadata for one profile/runtime model set.

    The catalog deliberately contains no model paths, download URLs, weights,
    credentials, or provider connection details.  Artifact hashes and pinned
    revisions are inherited from the endpoint identity types.
    """

    catalog_id: str
    profile_id: str
    runtime: str
    capabilities: tuple[EndpointCapability, ...]
    resource_estimate: ModelResourceEstimate
    embedding: EmbeddingIdentity | None = None
    rerank: RerankIdentity | None = None
    schema_version: str = MODEL_CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_CATALOG_SCHEMA_VERSION:
            raise ReleaseBundleError("model_catalog_schema_unsupported")
        _require_catalog_reference(self.catalog_id, "model_catalog_id_invalid")
        if profile_by_id(self.profile_id) is None:
            raise ReleaseBundleError("model_catalog_profile_unsupported")
        allowed_profiles = _RUNTIME_PROFILE_COMPATIBILITY.get(self.runtime)
        if allowed_profiles is None:
            raise ReleaseBundleError("model_catalog_runtime_unsupported")
        if self.profile_id not in allowed_profiles:
            raise ReleaseBundleError("model_catalog_profile_runtime_incompatible")
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise ReleaseBundleError("model_catalog_capabilities_required")
        if not all(isinstance(item, EndpointCapability) for item in self.capabilities):
            raise ReleaseBundleError("model_catalog_capability_invalid")
        capability_kinds = tuple(item.kind for item in self.capabilities)
        if len(set(capability_kinds)) != len(capability_kinds):
            raise ReleaseBundleError("model_catalog_capability_duplicate")
        if set(capability_kinds) - _COMPUTE_CAPABILITY_KINDS:
            raise ReleaseBundleError("model_catalog_capability_unsupported")
        if not isinstance(self.resource_estimate, ModelResourceEstimate):
            raise ReleaseBundleError("model_catalog_resource_invalid")
        if self.embedding is not None and not isinstance(self.embedding, EmbeddingIdentity):
            raise ReleaseBundleError("model_catalog_embedding_identity_invalid")
        if self.rerank is not None and not isinstance(self.rerank, RerankIdentity):
            raise ReleaseBundleError("model_catalog_rerank_identity_invalid")
        if ("embedding" in capability_kinds) != (self.embedding is not None):
            raise ReleaseBundleError("model_catalog_embedding_identity_mismatch")
        if ("rerank" in capability_kinds) != (self.rerank is not None):
            raise ReleaseBundleError("model_catalog_rerank_identity_mismatch")
        for identity in (self.embedding, self.rerank):
            if identity is not None and not _identity_model_is_safe(identity.model):
                raise ReleaseBundleError("model_catalog_model_reference_unsafe")

    @property
    def capability_contracts(self) -> tuple[str, ...]:
        """Return sorted protocol capabilities such as ``embedding/v1``."""

        return _capability_contracts(self.capabilities)

    def canonical_payload(self) -> dict[str, object]:
        """Return the non-recursive payload used for deterministic identity."""

        return {
            "schema_version": self.schema_version,
            "catalog_id": self.catalog_id,
            "profile": self.profile_id,
            "runtime": self.runtime,
            "resource_estimate": self.resource_estimate.to_dict(),
            "capabilities": [
                item.to_dict() for item in sorted(self.capabilities, key=lambda item: item.kind)
            ],
            "embedding": self.embedding.to_dict() if self.embedding is not None else None,
            "rerank": self.rerank.to_dict() if self.rerank is not None else None,
        }

    @property
    def digest(self) -> str:
        """Return a deterministic semantic digest for catalog binding."""

        return _digest(self.canonical_payload())

    def to_dict(self) -> dict[str, object]:
        payload = self.canonical_payload()
        payload["digest"] = self.digest
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> ModelCatalog:
        """Parse a strict, non-secret operator catalog without any network I/O."""

        mapping = _require_mapping(payload, "model_catalog_payload_invalid")
        _reject_secrets(mapping)
        _require_fields(
            mapping,
            required=frozenset(
                {
                    "schema_version",
                    "catalog_id",
                    "profile",
                    "runtime",
                    "resource_estimate",
                    "capabilities",
                    "embedding",
                    "rerank",
                }
            ),
            optional=frozenset({"digest"}),
            code="model_catalog_fields_invalid",
        )
        capabilities_payload = _require_sequence(
            mapping["capabilities"], "model_catalog_capabilities_invalid"
        )
        capabilities: list[EndpointCapability] = []
        for item in capabilities_payload:
            capability = _require_mapping(item, "model_catalog_capability_invalid")
            _require_fields(
                capability,
                required=frozenset({"kind", "contract_version"}),
                code="model_catalog_capability_fields_invalid",
            )
            try:
                capabilities.append(
                    EndpointCapability(
                        kind=capability["kind"], contract_version=capability["contract_version"]
                    )
                )
            except ValueError as exc:
                raise ReleaseBundleError("model_catalog_capability_invalid") from exc
        catalog = cls(
            catalog_id=mapping["catalog_id"],
            profile_id=mapping["profile"],
            runtime=mapping["runtime"],
            capabilities=tuple(capabilities),
            resource_estimate=ModelResourceEstimate.from_dict(mapping["resource_estimate"]),
            embedding=_embedding_identity_from_dict(mapping["embedding"]),
            rerank=_rerank_identity_from_dict(mapping["rerank"]),
            schema_version=mapping["schema_version"],
        )
        if (
            "digest" in mapping
            and _require_sha256(mapping["digest"], "model_catalog_digest_invalid") != catalog.digest
        ):
            raise ReleaseBundleError("model_catalog_digest_mismatch")
        return catalog


def _embedding_identity_from_dict(payload: object) -> EmbeddingIdentity | None:
    if payload is None:
        return None
    mapping = _require_mapping(payload, "model_catalog_embedding_identity_invalid")
    _require_fields(
        mapping,
        required=frozenset(
            {
                "model",
                "revision",
                "dimension",
                "normalization",
                "metric",
                "tokenization",
                "pooling",
                "artifact_sha256",
                "golden_vector_sha256",
            }
        ),
        code="model_catalog_embedding_identity_fields_invalid",
    )
    try:
        return EmbeddingIdentity(
            model=mapping["model"],
            revision=mapping["revision"],
            dimension=mapping["dimension"],
            normalization=mapping["normalization"],
            metric=mapping["metric"],
            tokenization=mapping["tokenization"],
            pooling=mapping["pooling"],
            artifact_sha256=mapping["artifact_sha256"],
            golden_vector_sha256=mapping["golden_vector_sha256"],
        )
    except ValueError as exc:
        raise ReleaseBundleError("model_catalog_embedding_identity_invalid") from exc


def _rerank_identity_from_dict(payload: object) -> RerankIdentity | None:
    if payload is None:
        return None
    mapping = _require_mapping(payload, "model_catalog_rerank_identity_invalid")
    _require_fields(
        mapping,
        required=frozenset({"model", "revision", "artifact_sha256", "scoring_schema"}),
        code="model_catalog_rerank_identity_fields_invalid",
    )
    try:
        return RerankIdentity(
            model=mapping["model"],
            revision=mapping["revision"],
            artifact_sha256=mapping["artifact_sha256"],
            scoring_schema=mapping["scoring_schema"],
        )
    except ValueError as exc:
        raise ReleaseBundleError("model_catalog_rerank_identity_invalid") from exc


@dataclass(frozen=True)
class ArtifactSbomReceipt:
    """One opaque CycloneDX file bound to an exact inspected OCI image.

    CycloneDX inventories intentionally remain scanner-owned opaque content.
    This typed receipt is the stable, release-owned assertion which says which
    inspected OCI image and platform that content describes.  Its trust root is
    the separately verified protected-workflow attestation of the receipt set.
    """

    artifact_id: str
    role: str
    platform: str
    variant: str
    oci_layout_digest: str
    image_digest: str
    sbom_digest: str
    sbom_size_bytes: int
    sbom_format: str
    sbom_spec_version: str
    scanner: str = "syft-oci-archive"

    def __post_init__(self) -> None:
        _require_catalog_reference(self.artifact_id, "artifact_sbom_receipt_id_invalid")
        if self.role not in _ARTIFACT_ROLES:
            raise ReleaseBundleError("artifact_sbom_receipt_role_invalid")
        if self.platform not in _SUPPORTED_PLATFORMS:
            raise ReleaseBundleError("artifact_sbom_receipt_platform_invalid")
        if self.role == PP_COMPUTE_NODE:
            if self.variant not in _COMPUTE_VARIANTS:
                raise ReleaseBundleError("artifact_sbom_receipt_variant_invalid")
            if self.variant == "cuda" and self.platform != "linux/amd64":
                raise ReleaseBundleError("artifact_sbom_receipt_cuda_platform_invalid")
        elif self.variant != "standard":
            raise ReleaseBundleError("artifact_sbom_receipt_role_matrix_invalid")
        expected_id = "-".join(
            (self.role.removeprefix("pp-"), self.platform.replace("/", "-"), self.variant)
        )
        if self.artifact_id != expected_id:
            raise ReleaseBundleError("artifact_sbom_receipt_id_matrix_mismatch")
        for value, code in (
            (self.oci_layout_digest, "artifact_sbom_receipt_layout_digest_invalid"),
            (self.image_digest, "artifact_sbom_receipt_image_digest_invalid"),
            (self.sbom_digest, "artifact_sbom_receipt_sbom_digest_invalid"),
        ):
            _require_sha256(value, code)
        _require_resource_bytes(
            self.sbom_size_bytes, "artifact_sbom_receipt_sbom_size_invalid", allow_zero=False
        )
        if self.sbom_format != "CycloneDX":
            raise ReleaseBundleError("artifact_sbom_receipt_format_invalid")
        if (
            not isinstance(self.sbom_spec_version, str)
            or _SBOM_SPEC_VERSION.fullmatch(self.sbom_spec_version) is None
        ):
            raise ReleaseBundleError("artifact_sbom_receipt_spec_version_invalid")
        if self.scanner != "syft-oci-archive":
            raise ReleaseBundleError("artifact_sbom_receipt_scanner_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "role": self.role,
            "platform": self.platform,
            "variant": self.variant,
            "oci_layout_digest": self.oci_layout_digest,
            "image_digest": self.image_digest,
            "sbom_digest": self.sbom_digest,
            "sbom_size_bytes": self.sbom_size_bytes,
            "sbom_format": self.sbom_format,
            "sbom_spec_version": self.sbom_spec_version,
            "scanner": self.scanner,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ArtifactSbomReceipt:
        mapping = _require_mapping(payload, "artifact_sbom_receipt_payload_invalid")
        _reject_secrets(mapping)
        _require_fields(
            mapping,
            required=frozenset(
                {
                    "artifact_id",
                    "role",
                    "platform",
                    "variant",
                    "oci_layout_digest",
                    "image_digest",
                    "sbom_digest",
                    "sbom_size_bytes",
                    "sbom_format",
                    "sbom_spec_version",
                    "scanner",
                }
            ),
            code="artifact_sbom_receipt_fields_invalid",
        )
        return cls(
            artifact_id=mapping["artifact_id"],
            role=mapping["role"],
            platform=mapping["platform"],
            variant=mapping["variant"],
            oci_layout_digest=mapping["oci_layout_digest"],
            image_digest=mapping["image_digest"],
            sbom_digest=mapping["sbom_digest"],
            sbom_size_bytes=mapping["sbom_size_bytes"],
            sbom_format=mapping["sbom_format"],
            sbom_spec_version=mapping["sbom_spec_version"],
            scanner=mapping["scanner"],
        )


@dataclass(frozen=True)
class ArtifactSbomReceiptSet:
    """Canonical, attested collection of exact OCI-to-SBOM associations."""

    receipts: tuple[ArtifactSbomReceipt, ...]
    schema_version: str = ARTIFACT_SBOM_RECEIPTS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ARTIFACT_SBOM_RECEIPTS_SCHEMA_VERSION:
            raise ReleaseBundleError("artifact_sbom_receipts_schema_unsupported")
        if not isinstance(self.receipts, tuple) or not self.receipts:
            raise ReleaseBundleError("artifact_sbom_receipts_invalid")
        if not all(isinstance(item, ArtifactSbomReceipt) for item in self.receipts):
            raise ReleaseBundleError("artifact_sbom_receipts_invalid")
        artifact_ids = tuple(item.artifact_id for item in self.receipts)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ReleaseBundleError("artifact_sbom_receipts_duplicate")
        object.__setattr__(
            self,
            "receipts",
            tuple(sorted(self.receipts, key=lambda item: item.artifact_id)),
        )

    @property
    def by_artifact_id(self) -> Mapping[str, ArtifactSbomReceipt]:
        return {item.artifact_id: item for item in self.receipts}

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "receipts": [
                item.to_dict() for item in sorted(self.receipts, key=lambda item: item.artifact_id)
            ],
        }

    @property
    def digest(self) -> str:
        """Digest the exact canonical file representation which CI attests."""

        return _digest(self.canonical_payload())

    def to_dict(self) -> dict[str, object]:
        return self.canonical_payload()

    @classmethod
    def from_dict(cls, payload: object) -> ArtifactSbomReceiptSet:
        mapping = _require_mapping(payload, "artifact_sbom_receipts_payload_invalid")
        _reject_secrets(mapping)
        _require_fields(
            mapping,
            required=frozenset({"schema_version", "receipts"}),
            code="artifact_sbom_receipts_fields_invalid",
        )
        receipts_payload = _require_sequence(mapping["receipts"], "artifact_sbom_receipts_invalid")
        return cls(
            receipts=tuple(ArtifactSbomReceipt.from_dict(item) for item in receipts_payload),
            schema_version=mapping["schema_version"],
        )


@dataclass(frozen=True)
class ImmutableArtifactBinding:
    """One fully expanded immutable OCI materialization in a release bundle."""

    artifact_id: str
    role: str
    platform: str
    variant: str
    capabilities: tuple[str, ...]
    immutable_reference: str
    image_digest: str
    oci_layout_digest: str
    oci_labels_digest: str
    embedded_sbom_digest: str
    provenance_digest: str
    evidence_receipt_digest: str
    sbom_digest: str

    def __post_init__(self) -> None:
        _require_catalog_reference(self.artifact_id, "immutable_artifact_id_invalid")
        if self.role not in _ARTIFACT_ROLES:
            raise ReleaseBundleError("immutable_artifact_role_invalid")
        if self.platform not in _SUPPORTED_PLATFORMS:
            raise ReleaseBundleError("immutable_artifact_platform_invalid")
        if self.role == PP_COMPUTE_NODE:
            if self.variant not in _COMPUTE_VARIANTS:
                raise ReleaseBundleError("immutable_artifact_variant_invalid")
            if self.variant == "cuda" and self.platform != "linux/amd64":
                raise ReleaseBundleError("immutable_artifact_cuda_platform_invalid")
            if self.capabilities != _COMPUTE_PACKAGE_MANIFEST.capability_contracts:
                raise ReleaseBundleError("immutable_artifact_capability_invalid")
        elif self.variant != "standard" or self.capabilities:
            raise ReleaseBundleError("immutable_artifact_role_matrix_invalid")
        expected_id = "-".join(
            (self.role.removeprefix("pp-"), self.platform.replace("/", "-"), self.variant)
        )
        if self.artifact_id != expected_id:
            raise ReleaseBundleError("immutable_artifact_id_matrix_mismatch")
        if not isinstance(self.capabilities, tuple) or len(set(self.capabilities)) != len(
            self.capabilities
        ):
            raise ReleaseBundleError("immutable_artifact_capability_invalid")
        if any(_CAPABILITY.fullmatch(item) is None for item in self.capabilities):
            raise ReleaseBundleError("immutable_artifact_capability_invalid")
        _require_sha256(self.image_digest, "immutable_artifact_image_digest_invalid")
        if self.immutable_reference != f"oci@{self.image_digest}":
            raise ReleaseBundleError("immutable_artifact_reference_digest_mismatch")
        for value, code in (
            (self.oci_layout_digest, "immutable_artifact_oci_layout_digest_invalid"),
            (self.oci_labels_digest, "immutable_artifact_labels_digest_invalid"),
            (self.embedded_sbom_digest, "immutable_artifact_embedded_sbom_digest_invalid"),
            (self.provenance_digest, "immutable_artifact_provenance_digest_invalid"),
            (self.evidence_receipt_digest, "immutable_artifact_evidence_receipt_digest_invalid"),
            (self.sbom_digest, "immutable_artifact_sbom_digest_invalid"),
        ):
            _require_sha256(value, code)

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "role": self.role,
            "platform": self.platform,
            "variant": self.variant,
            "capabilities": list(self.capabilities),
            "immutable_reference": self.immutable_reference,
            "image_digest": self.image_digest,
            "oci_layout_digest": self.oci_layout_digest,
            "oci_labels_digest": self.oci_labels_digest,
            "embedded_sbom_digest": self.embedded_sbom_digest,
            "provenance_digest": self.provenance_digest,
            "evidence_receipt_digest": self.evidence_receipt_digest,
            "sbom_digest": self.sbom_digest,
        }

    def materialization_projection(self) -> dict[str, str]:
        """Return the exact no-capability materialization evidence projection."""

        return {
            "artifact_id": self.artifact_id,
            "role": self.role,
            "platform": self.platform,
            "variant": self.variant,
            "immutable_reference": self.immutable_reference,
            "image_digest": self.image_digest,
            "oci_layout_digest": self.oci_layout_digest,
            "oci_labels_digest": self.oci_labels_digest,
            "embedded_sbom_digest": self.embedded_sbom_digest,
            "provenance_digest": self.provenance_digest,
            "evidence_receipt_digest": self.evidence_receipt_digest,
            "sbom_digest": self.sbom_digest,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ImmutableArtifactBinding:
        mapping = _require_mapping(payload, "immutable_artifact_payload_invalid")
        _reject_secrets(mapping)
        _require_fields(
            mapping,
            required=frozenset(
                {
                    "artifact_id",
                    "role",
                    "platform",
                    "variant",
                    "capabilities",
                    "immutable_reference",
                    "image_digest",
                    "oci_layout_digest",
                    "oci_labels_digest",
                    "embedded_sbom_digest",
                    "provenance_digest",
                    "evidence_receipt_digest",
                    "sbom_digest",
                }
            ),
            code="immutable_artifact_fields_invalid",
        )
        capabilities = _require_sequence(
            mapping["capabilities"], "immutable_artifact_capability_invalid"
        )
        if not all(isinstance(item, str) for item in capabilities):
            raise ReleaseBundleError("immutable_artifact_capability_invalid")
        return cls(
            artifact_id=mapping["artifact_id"],
            role=mapping["role"],
            platform=mapping["platform"],
            variant=mapping["variant"],
            capabilities=tuple(capabilities),
            immutable_reference=mapping["immutable_reference"],
            image_digest=mapping["image_digest"],
            oci_layout_digest=mapping["oci_layout_digest"],
            oci_labels_digest=mapping["oci_labels_digest"],
            embedded_sbom_digest=mapping["embedded_sbom_digest"],
            provenance_digest=mapping["provenance_digest"],
            evidence_receipt_digest=mapping["evidence_receipt_digest"],
            sbom_digest=mapping["sbom_digest"],
        )


@dataclass(frozen=True)
class ArtifactBundleBinding:
    """Secret-free release projection of one already-inspected artifact bundle."""

    profile_id: str
    source_revision: str
    package_version: str
    model_catalog_reference: str
    model_catalog_digest: str
    artifact_policy_digest: str
    artifact_sbom_receipts_digest: str
    artifacts: tuple[ImmutableArtifactBinding, ...]
    compute_capabilities: tuple[str, ...]
    schema_version: str = ARTIFACT_BUNDLE_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ARTIFACT_BUNDLE_BINDING_SCHEMA_VERSION:
            raise ReleaseBundleError("artifact_bundle_binding_schema_unsupported")
        if profile_by_id(self.profile_id) is None:
            raise ReleaseBundleError("artifact_bundle_binding_profile_unsupported")
        _require_source_sha(self.source_revision, "artifact_bundle_binding_source_revision_invalid")
        _require_package_version(
            self.package_version, "artifact_bundle_binding_package_version_invalid"
        )
        _require_catalog_reference(
            self.model_catalog_reference,
            "artifact_bundle_binding_catalog_reference_invalid",
        )
        _require_sha256(self.model_catalog_digest, "artifact_bundle_binding_catalog_digest_invalid")
        _require_sha256(
            self.artifact_policy_digest, "artifact_bundle_binding_policy_digest_invalid"
        )
        _require_sha256(
            self.artifact_sbom_receipts_digest,
            "artifact_bundle_binding_sbom_receipts_digest_invalid",
        )
        if not isinstance(self.artifacts, tuple) or not self.artifacts:
            raise ReleaseBundleError("artifact_bundle_binding_artifacts_invalid")
        if not all(isinstance(item, ImmutableArtifactBinding) for item in self.artifacts):
            raise ReleaseBundleError("artifact_bundle_binding_artifact_invalid")
        artifact_ids = tuple(item.artifact_id for item in self.artifacts)
        targets = tuple((item.role, item.platform, item.variant) for item in self.artifacts)
        if len(set(artifact_ids)) != len(artifact_ids) or len(set(targets)) != len(targets):
            raise ReleaseBundleError("artifact_bundle_binding_artifact_duplicate")
        if not isinstance(self.compute_capabilities, tuple):
            raise ReleaseBundleError("artifact_bundle_binding_capability_invalid")
        if len(set(self.compute_capabilities)) != len(self.compute_capabilities):
            raise ReleaseBundleError("artifact_bundle_binding_capability_duplicate")
        if any(_CAPABILITY.fullmatch(item) is None for item in self.compute_capabilities):
            raise ReleaseBundleError("artifact_bundle_binding_capability_invalid")
        if set(self.compute_capabilities) - _COMPUTE_CAPABILITY_CONTRACTS:
            raise ReleaseBundleError("artifact_bundle_binding_capability_unsupported")
        self._validate_artifact_matrix()

    @property
    def artifact_ids(self) -> tuple[str, ...]:
        """Return the immutable artifact identifiers without hiding their evidence."""

        return tuple(item.artifact_id for item in self.artifacts)

    def _validate_artifact_matrix(self) -> None:
        by_target = {(item.role, item.platform, item.variant): item for item in self.artifacts}
        platforms = {item.platform for item in self.artifacts}
        if not platforms:
            raise ReleaseBundleError("artifact_bundle_binding_matrix_empty")
        for platform in platforms:
            for role in (PP_LOCAL_EDGE, PP_SERVER_BACKEND):
                if (role, platform, "standard") not in by_target:
                    raise ReleaseBundleError("artifact_bundle_binding_matrix_incomplete")
        compute_artifacts = tuple(item for item in self.artifacts if item.role == PP_COMPUTE_NODE)
        if self.profile_id == "local-cloud" and compute_artifacts:
            raise ReleaseBundleError("artifact_bundle_binding_cloud_compute_forbidden")
        if self.profile_id == "split-accelerated" and not compute_artifacts:
            raise ReleaseBundleError("artifact_bundle_binding_compute_required")
        variants = {item.variant for item in compute_artifacts}
        for platform in platforms:
            expected = {"cpu"} if "cpu" in variants else set()
            if "cuda" in variants and platform == "linux/amd64":
                expected.add("cuda")
            actual = {
                variant
                for role, target_platform, variant in by_target
                if role == PP_COMPUTE_NODE and target_platform == platform
            }
            if actual != expected:
                raise ReleaseBundleError("artifact_bundle_binding_compute_matrix_mismatch")
        expected_capabilities = tuple(
            sorted({capability for item in compute_artifacts for capability in item.capabilities})
        )
        if tuple(sorted(self.compute_capabilities)) != expected_capabilities:
            raise ReleaseBundleError("artifact_bundle_binding_capability_matrix_mismatch")

    def validate_artifact_sbom_receipts(self, receipts: ArtifactSbomReceiptSet) -> None:
        """Fail closed unless one attested receipt matches every materialization."""

        if not isinstance(receipts, ArtifactSbomReceiptSet):
            raise ReleaseBundleError("artifact_bundle_binding_sbom_receipts_invalid")
        if receipts.digest != self.artifact_sbom_receipts_digest:
            raise ReleaseBundleError("artifact_bundle_binding_sbom_receipts_digest_mismatch")
        receipts_by_id = receipts.by_artifact_id
        if set(receipts_by_id) != set(self.artifact_ids):
            raise ReleaseBundleError("artifact_bundle_binding_sbom_receipts_matrix_mismatch")
        for artifact in self.artifacts:
            receipt = receipts_by_id[artifact.artifact_id]
            if (
                receipt.role,
                receipt.platform,
                receipt.variant,
                receipt.oci_layout_digest,
                receipt.image_digest,
                receipt.sbom_digest,
            ) != (
                artifact.role,
                artifact.platform,
                artifact.variant,
                artifact.oci_layout_digest,
                artifact.image_digest,
                artifact.sbom_digest,
            ):
                raise ReleaseBundleError("artifact_bundle_binding_sbom_receipts_artifact_mismatch")

    @classmethod
    def from_artifact_bundle(
        cls,
        artifact_bundle: ArtifactBundle,
        *,
        artifact_sbom_receipts: ArtifactSbomReceiptSet,
    ) -> ArtifactBundleBinding:
        """Bind one validated compiler result without exposing its mutable inputs."""

        from .container_artifacts import ArtifactBundle

        if not isinstance(artifact_bundle, ArtifactBundle):
            raise ReleaseBundleError("artifact_bundle_binding_input_invalid")
        if not isinstance(artifact_sbom_receipts, ArtifactSbomReceiptSet):
            raise ReleaseBundleError("artifact_bundle_binding_sbom_receipts_invalid")
        request = artifact_bundle.plan.request
        if request.model_catalog_reference is None or request.model_catalog_digest is None:
            raise ReleaseBundleError("artifact_bundle_binding_catalog_required")
        if _SOURCE_SHA.fullmatch(request.source_revision) is None:
            raise ReleaseBundleError("artifact_bundle_binding_source_revision_invalid")
        descriptors = {item.artifact_id: item for item in artifact_bundle.plan.artifacts}
        materializations = {item.artifact_id: item for item in artifact_bundle.materializations}
        receipts = artifact_sbom_receipts.by_artifact_id
        if set(descriptors) != set(materializations):
            raise ReleaseBundleError("artifact_bundle_binding_artifact_set_mismatch")
        if set(receipts) != set(materializations):
            raise ReleaseBundleError("artifact_bundle_binding_sbom_receipts_matrix_mismatch")
        artifacts = tuple(
            ImmutableArtifactBinding(
                artifact_id=artifact_id,
                role=materializations[artifact_id].role,
                platform=materializations[artifact_id].platform,
                variant=materializations[artifact_id].variant,
                capabilities=descriptors[artifact_id].capabilities,
                immutable_reference=materializations[artifact_id].immutable_reference,
                image_digest=materializations[artifact_id].image_digest,
                oci_layout_digest=materializations[artifact_id].oci_layout_digest,
                oci_labels_digest=materializations[artifact_id].oci_labels_digest,
                embedded_sbom_digest=materializations[artifact_id].sbom_digest,
                provenance_digest=materializations[artifact_id].provenance_digest,
                evidence_receipt_digest=materializations[artifact_id].evidence_receipt.digest,
                sbom_digest=receipts[artifact_id].sbom_digest,
            )
            for artifact_id in sorted(materializations)
        )
        compute_capabilities = tuple(
            sorted(
                {
                    capability
                    for item in artifacts
                    if item.role == PP_COMPUTE_NODE
                    for capability in item.capabilities
                }
            )
        )
        binding = cls(
            profile_id=request.profile_id,
            source_revision=request.source_revision,
            package_version=request.package_version,
            model_catalog_reference=request.model_catalog_reference,
            model_catalog_digest=request.model_catalog_digest,
            artifact_policy_digest=artifact_bundle.plan.policy_digest,
            artifact_sbom_receipts_digest=artifact_sbom_receipts.digest,
            artifacts=artifacts,
            compute_capabilities=compute_capabilities,
        )
        binding.validate_artifact_sbom_receipts(artifact_sbom_receipts)
        return binding

    def _artifact_evidence_payload(self) -> dict[str, object]:
        """Canonical expanded materialization receipt used for artifact evidence."""

        artifact_payloads = [
            item.materialization_projection()
            for item in sorted(self.artifacts, key=lambda item: item.artifact_id)
        ]
        return {
            "schema_version": CONTAINER_ARTIFACT_BUNDLE_SCHEMA_VERSION,
            "policy_digest": self.artifact_policy_digest,
            "artifacts": artifact_payloads,
            "inspection": {
                "schema_version": CONTAINER_ARTIFACT_BUNDLE_SCHEMA_VERSION,
                "policy_digest": self.artifact_policy_digest,
                "artifact_ids": [item["artifact_id"] for item in artifact_payloads],
                "outcome": "pass",
            },
        }

    @property
    def artifact_bundle_digest(self) -> str:
        """Digest the complete expanded immutable artifact evidence projection."""

        return _digest(self._artifact_evidence_payload())

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile": self.profile_id,
            "source_revision": self.source_revision,
            "package_version": self.package_version,
            "model_catalog_reference": self.model_catalog_reference,
            "model_catalog_digest": self.model_catalog_digest,
            "artifact_policy_digest": self.artifact_policy_digest,
            "artifact_sbom_receipts_digest": self.artifact_sbom_receipts_digest,
            "artifact_bundle_digest": self.artifact_bundle_digest,
            "artifacts": [
                item.to_dict() for item in sorted(self.artifacts, key=lambda item: item.artifact_id)
            ],
            "compute_capabilities": sorted(self.compute_capabilities),
        }

    @property
    def digest(self) -> str:
        """Return the canonical digest of this binding, not the OCI image digest."""

        return _digest(self.canonical_payload())

    def to_dict(self) -> dict[str, object]:
        payload = self.canonical_payload()
        payload["binding_digest"] = self.digest
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> ArtifactBundleBinding:
        """Parse a strict expanded binding without accepting opaque artifact data."""

        mapping = _require_mapping(payload, "artifact_bundle_binding_payload_invalid")
        _reject_secrets(mapping)
        _require_fields(
            mapping,
            required=frozenset(
                {
                    "schema_version",
                    "profile",
                    "source_revision",
                    "package_version",
                    "model_catalog_reference",
                    "model_catalog_digest",
                    "artifact_policy_digest",
                    "artifact_sbom_receipts_digest",
                    "artifacts",
                    "compute_capabilities",
                }
            ),
            optional=frozenset({"artifact_bundle_digest", "binding_digest"}),
            code="artifact_bundle_binding_fields_invalid",
        )
        artifacts_payload = _require_sequence(
            mapping["artifacts"], "artifact_bundle_binding_artifacts_invalid"
        )
        capabilities_payload = _require_sequence(
            mapping["compute_capabilities"], "artifact_bundle_binding_capability_invalid"
        )
        if not all(isinstance(item, str) for item in capabilities_payload):
            raise ReleaseBundleError("artifact_bundle_binding_capability_invalid")
        binding = cls(
            profile_id=mapping["profile"],
            source_revision=mapping["source_revision"],
            package_version=mapping["package_version"],
            model_catalog_reference=mapping["model_catalog_reference"],
            model_catalog_digest=mapping["model_catalog_digest"],
            artifact_policy_digest=mapping["artifact_policy_digest"],
            artifact_sbom_receipts_digest=mapping["artifact_sbom_receipts_digest"],
            artifacts=tuple(ImmutableArtifactBinding.from_dict(item) for item in artifacts_payload),
            compute_capabilities=tuple(capabilities_payload),
            schema_version=mapping["schema_version"],
        )
        if (
            "artifact_bundle_digest" in mapping
            and _require_sha256(
                mapping["artifact_bundle_digest"], "artifact_bundle_binding_digest_invalid"
            )
            != binding.artifact_bundle_digest
        ):
            raise ReleaseBundleError("artifact_bundle_binding_digest_mismatch")
        if (
            "binding_digest" in mapping
            and _require_sha256(mapping["binding_digest"], "artifact_bundle_binding_digest_invalid")
            != binding.digest
        ):
            raise ReleaseBundleError("artifact_bundle_binding_digest_mismatch")
        return binding


@dataclass(frozen=True)
class VerifiedEvidenceProjection:
    """Non-secret acknowledgement that another adapter already verified evidence.

    The projection intentionally has no signature, certificate, URL, token, or
    opaque provider response field.  It verifies neither a signature nor an
    attestation; its caller must only create it after that work has succeeded.
    """

    subject: str
    subject_digest: str
    attestation_digest: str
    predicate_type: str
    verifier: str
    verified: bool = True
    schema_version: str = RELEASE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RELEASE_EVIDENCE_SCHEMA_VERSION:
            raise ReleaseBundleError("release_evidence_schema_unsupported")
        if self.subject not in _EVIDENCE_SUBJECTS:
            raise ReleaseBundleError("release_evidence_subject_unsupported")
        _require_sha256(self.subject_digest, "release_evidence_subject_digest_invalid")
        _require_sha256(self.attestation_digest, "release_evidence_attestation_digest_invalid")
        if _CAPABILITY.fullmatch(self.predicate_type) is None:
            raise ReleaseBundleError("release_evidence_predicate_invalid")
        _require_reference(self.verifier, "release_evidence_verifier_invalid")
        if self.verified is not True:
            raise ReleaseBundleError("release_evidence_not_verified")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "subject": self.subject,
            "subject_digest": self.subject_digest,
            "attestation_digest": self.attestation_digest,
            "predicate_type": self.predicate_type,
            "verifier": self.verifier,
            "verified": True,
        }

    @classmethod
    def from_dict(cls, payload: object) -> VerifiedEvidenceProjection:
        """Parse a strict verification projection without accepting proof material."""

        mapping = _require_mapping(payload, "release_evidence_payload_invalid")
        _reject_secrets(mapping)
        _require_fields(
            mapping,
            required=frozenset(
                {
                    "schema_version",
                    "subject",
                    "subject_digest",
                    "attestation_digest",
                    "predicate_type",
                    "verifier",
                    "verified",
                }
            ),
            code="release_evidence_fields_invalid",
        )
        return cls(
            subject=mapping["subject"],
            subject_digest=mapping["subject_digest"],
            attestation_digest=mapping["attestation_digest"],
            predicate_type=mapping["predicate_type"],
            verifier=mapping["verifier"],
            verified=mapping["verified"],
            schema_version=mapping["schema_version"],
        )


@dataclass(frozen=True)
class ReleaseBundleIdentity:
    """Minimal immutable identity of a prior bundle eligible for rollback."""

    release_version: str
    source_revision: str
    bundle_digest: str
    schema_version: str = RELEASE_BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RELEASE_BUNDLE_SCHEMA_VERSION:
            raise ReleaseBundleError("release_bundle_identity_schema_unsupported")
        _require_release_version(self.release_version, "release_bundle_identity_version_invalid")
        _require_source_sha(self.source_revision, "release_bundle_identity_source_revision_invalid")
        _require_sha256(self.bundle_digest, "release_bundle_identity_digest_invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "release_version": self.release_version,
            "source_revision": self.source_revision,
            "bundle_digest": self.bundle_digest,
        }


@dataclass(frozen=True)
class ReleaseBundle:
    """Immutable release contract binding artifacts, model identity, and evidence."""

    release_version: str
    source_revision: str
    release_manifest_sha256: str
    artifact_binding: ArtifactBundleBinding
    model_catalog: ModelCatalog
    attestation_evidence: tuple[VerifiedEvidenceProjection, ...]
    previous_bundle: ReleaseBundleIdentity | None = None
    schema_version: str = RELEASE_BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RELEASE_BUNDLE_SCHEMA_VERSION:
            raise ReleaseBundleError("release_bundle_schema_unsupported")
        _require_release_version(self.release_version, "release_bundle_version_invalid")
        expected_package_version = package_version_for_release(self.release_version)
        _require_source_sha(self.source_revision, "release_bundle_source_revision_invalid")
        _require_raw_sha256(
            self.release_manifest_sha256,
            "release_bundle_release_manifest_sha256_invalid",
        )
        if not isinstance(self.artifact_binding, ArtifactBundleBinding):
            raise ReleaseBundleError("release_bundle_artifact_binding_invalid")
        if not isinstance(self.model_catalog, ModelCatalog):
            raise ReleaseBundleError("release_bundle_model_catalog_invalid")
        if not isinstance(self.attestation_evidence, tuple) or not all(
            isinstance(item, VerifiedEvidenceProjection) for item in self.attestation_evidence
        ):
            raise ReleaseBundleError("release_bundle_evidence_invalid")
        if self.previous_bundle is not None and not isinstance(
            self.previous_bundle, ReleaseBundleIdentity
        ):
            raise ReleaseBundleError("release_bundle_rollback_identity_invalid")
        if self.artifact_binding.source_revision != self.source_revision:
            raise ReleaseBundleError("release_bundle_artifact_source_mismatch")
        if self.artifact_binding.package_version != expected_package_version:
            raise ReleaseBundleError("release_bundle_package_version_mismatch")
        if self.artifact_binding.profile_id != self.model_catalog.profile_id:
            raise ReleaseBundleError("release_bundle_profile_mismatch")
        if self.artifact_binding.model_catalog_reference != self.model_catalog.catalog_id:
            raise ReleaseBundleError("release_bundle_catalog_reference_mismatch")
        if self.artifact_binding.model_catalog_digest != self.model_catalog.digest:
            raise ReleaseBundleError("release_bundle_catalog_digest_mismatch")
        self._validate_runtime_artifact_compatibility()
        self._validate_evidence()
        if self.previous_bundle is not None and self.previous_bundle.bundle_digest == self.digest:
            raise ReleaseBundleError("release_bundle_rollback_identity_self_reference")

    @property
    def release_manifest_digest(self) -> str:
        """Return the raw release-manifest SHA as a digest-form identifier."""

        return f"sha256:{self.release_manifest_sha256}"

    def _validate_runtime_artifact_compatibility(self) -> None:
        expected_capabilities = self.model_catalog.capability_contracts
        actual_capabilities = tuple(sorted(self.artifact_binding.compute_capabilities))
        if self.model_catalog.runtime == MODEL_RUNTIME_CLOUD:
            if actual_capabilities:
                raise ReleaseBundleError("release_bundle_cloud_compute_artifacts_forbidden")
            return
        if actual_capabilities != expected_capabilities:
            raise ReleaseBundleError("release_bundle_compute_capability_mismatch")

    def _validate_evidence(self) -> None:
        evidence_by_subject = {item.subject: item for item in self.attestation_evidence}
        if len(evidence_by_subject) != len(self.attestation_evidence):
            raise ReleaseBundleError("release_bundle_evidence_duplicate")
        if set(evidence_by_subject) != _EVIDENCE_SUBJECTS:
            raise ReleaseBundleError("release_bundle_evidence_set_invalid")
        expected_digests = {
            "release-manifest": self.release_manifest_digest,
            "model-catalog": self.model_catalog.digest,
            "artifact-bundle": self.artifact_binding.digest,
            "artifact-sbom-receipts": self.artifact_binding.artifact_sbom_receipts_digest,
        }
        if any(
            evidence_by_subject[subject].subject_digest != expected_digest
            for subject, expected_digest in expected_digests.items()
        ):
            raise ReleaseBundleError("release_bundle_evidence_subject_mismatch")

    def canonical_payload(self) -> dict[str, object]:
        """Return the non-recursive canonical semantics of this release bundle."""

        return {
            "schema_version": self.schema_version,
            "release": {
                "version": self.release_version,
                "channel": release_channel(self.release_version),
                "package_version": package_version_for_release(self.release_version),
            },
            "source_revision": self.source_revision,
            "release_manifest": {
                "sha256": self.release_manifest_sha256,
                "digest": self.release_manifest_digest,
            },
            "artifact_binding": self.artifact_binding.to_dict(),
            "model_catalog": self.model_catalog.to_dict(),
            "attestation_evidence": [
                item.to_dict()
                for item in sorted(self.attestation_evidence, key=lambda item: item.subject)
            ],
            "previous_bundle": self.previous_bundle.to_dict() if self.previous_bundle else None,
        }

    @property
    def digest(self) -> str:
        """Return the deterministic identity of the release bundle semantics."""

        return _digest(self.canonical_payload())

    @property
    def identity(self) -> ReleaseBundleIdentity:
        """Return the typed identity a later bundle may use for rollback."""

        return ReleaseBundleIdentity(
            release_version=self.release_version,
            source_revision=self.source_revision,
            bundle_digest=self.digest,
        )

    def to_dict(self) -> dict[str, object]:
        payload = self.canonical_payload()
        payload["bundle_digest"] = self.digest
        return payload


def canonical_model_catalog_bytes(catalog: ModelCatalog) -> bytes:
    """Return the single canonical serialization used for catalog identity."""

    return _canonical_json(catalog.canonical_payload())


def canonical_release_bundle_bytes(bundle: ReleaseBundle) -> bytes:
    """Return the single canonical serialization used for bundle identity."""

    return _canonical_json(bundle.canonical_payload())


__all__ = [
    "ARTIFACT_BUNDLE_BINDING_SCHEMA_VERSION",
    "ARTIFACT_SBOM_RECEIPTS_SCHEMA_VERSION",
    "MODEL_CATALOG_SCHEMA_VERSION",
    "MODEL_RUNTIME_CLOUD",
    "MODEL_RUNTIME_LOCAL",
    "MODEL_RUNTIME_REMOTE",
    "RELEASE_BUNDLE_SCHEMA_VERSION",
    "RELEASE_EVIDENCE_SCHEMA_VERSION",
    "ArtifactBundleBinding",
    "ArtifactSbomReceipt",
    "ArtifactSbomReceiptSet",
    "ImmutableArtifactBinding",
    "ModelCatalog",
    "ModelResourceEstimate",
    "ReleaseBundle",
    "ReleaseBundleError",
    "ReleaseBundleIdentity",
    "VerifiedEvidenceProjection",
    "canonical_model_catalog_bytes",
    "canonical_release_bundle_bytes",
]
