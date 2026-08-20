#!/usr/bin/env python3
"""Create a fail-closed, local-evidence RC release bundle.

This script is deliberately a *pre-attestation* seam.  It reads a tracked,
secret-free model catalog, inspects exact OCI-layout outputs already produced
by Buildx, and writes four deterministic release inputs:

* ``model-catalog.json``;
* ``artifact-sbom-receipts.json``;
* ``artifact-binding.json``; and
* ``release-bundle.json``.

The script never self-certifies evidence.  A final ``release-bundle.json``
is emitted only when the caller supplies a strict ``verified-evidence.json``
projection produced *after* an external attestation verifier succeeds.  The
projection's ``attestation_digest`` fields are deterministic SHA-256 hashes
of that verifier's saved reports; they are not invented local evidence.

No network, registry, Docker daemon, credential, publishing, deployment, or
runtime action is performed here.  OCI layouts are regular tar files passed
in by the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Keep the script runnable from a source checkout before the project is installed.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from plastic_promise.deployment import (  # noqa: E402
    COMPUTE_VARIANT_CPU,
    COMPUTE_VARIANT_CUDA,
    PP_COMPUTE_NODE,
    PP_LOCAL_EDGE,
    PP_SERVER_BACKEND,
    ArtifactBundleBinding,
    ArtifactEvidenceReceipt,
    ArtifactMaterialization,
    ArtifactRequest,
    ContainerArtifactCompiler,
    ContainerArtifactError,
    ModelCatalog,
    ReleaseBundle,
    ReleaseBundleError,
    VerifiedEvidenceProjection,
    canonical_model_catalog_bytes,
)
from plastic_promise.deployment.release_bundle import (  # noqa: E402
    ArtifactSbomReceipt,
    ArtifactSbomReceiptSet,
)
from plastic_promise.release_manifest import (  # noqa: E402
    ReleaseManifestError,
    package_version_for_release,
    sha256_file,
    validate_release_manifest,
)

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
_SUPPORTED_PROFILE = "split-accelerated"
_SUPPORTED_RUNTIME = "remote-inference"
_REQUIRED_CAPABILITIES = ("embedding/v1", "rerank/v1")
_PLATFORMS = ("linux/amd64", "linux/arm64")


class ReleaseBundleCreateError(ValueError):
    """A stable, non-secret command-line error for this release seam."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OciImage:
    """One platform-specific image descriptor found in an OCI layout."""

    digest: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class OciLayout:
    """One OCI layout with a single immutable root descriptor."""

    root_digest: str
    images_by_platform: Mapping[str, OciImage]


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _digest_payload(payload: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


def _file_digest(path: Path) -> str:
    try:
        return f"sha256:{sha256_file(path)}"
    except ReleaseManifestError as exc:
        raise ReleaseBundleCreateError("release_bundle_file_invalid") from exc


def _require_mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ReleaseBundleCreateError(code)
    return value


def _require_sequence(value: object, code: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ReleaseBundleCreateError(code)
    return value


def _require_digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReleaseBundleCreateError(code)
    return value


def _load_json(path: Path, code: str) -> object:
    if path.is_symlink() or not path.is_file():
        raise ReleaseBundleCreateError(code)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBundleCreateError(code) from exc


def _derive_artifact_sbom_receipts(
    values: Sequence[str],
    *,
    plan: Any,
    layouts: Mapping[tuple[str, str], OciLayout],
) -> ArtifactSbomReceiptSet:
    """Bind opaque Syft output to one inspected OCI artifact matrix.

    The SBOM's own scanner-defined metadata is deliberately not interpreted.
    The release-owned receipt records the stable artifact, platform, and OCI
    image identity after the corresponding layout descriptors have been
    hash-checked by :func:`_read_oci_layout`.
    """

    expected_artifacts = {artifact.artifact_id: artifact for artifact in plan.artifacts}
    sbom_inputs: dict[str, tuple[Path, Mapping[str, object]]] = {}
    for value in values:
        if not isinstance(value, str) or value.count("=") != 1:
            raise ReleaseBundleCreateError("release_bundle_artifact_sbom_argument_invalid")
        artifact_id, raw_path = value.split("=", maxsplit=1)
        if artifact_id not in expected_artifacts or not raw_path:
            raise ReleaseBundleCreateError("release_bundle_artifact_sbom_argument_invalid")
        if artifact_id in sbom_inputs:
            raise ReleaseBundleCreateError("release_bundle_artifact_sbom_duplicate")
        path = Path(raw_path)
        if path.suffix != ".json" or path.is_symlink() or not path.is_file():
            raise ReleaseBundleCreateError("release_bundle_artifact_sbom_invalid")
        payload = _require_mapping(
            _load_json(path, "release_bundle_artifact_sbom_invalid"),
            "release_bundle_artifact_sbom_invalid",
        )
        if payload.get("bomFormat") != "CycloneDX" or not isinstance(
            payload.get("specVersion"), str
        ):
            raise ReleaseBundleCreateError("release_bundle_artifact_sbom_invalid")
        sbom_inputs[artifact_id] = (path, payload)
    if set(sbom_inputs) != set(expected_artifacts):
        raise ReleaseBundleCreateError("release_bundle_artifact_sbom_matrix_incomplete")
    receipts: list[ArtifactSbomReceipt] = []
    for artifact_id, artifact in expected_artifacts.items():
        layout = layouts.get((artifact.role, artifact.variant))
        if layout is None:
            raise ReleaseBundleCreateError("release_bundle_oci_matrix_incomplete")
        image = layout.images_by_platform.get(artifact.platform)
        if image is None:
            raise ReleaseBundleCreateError("release_bundle_oci_matrix_incomplete")
        path, payload = sbom_inputs[artifact_id]
        try:
            receipts.append(
                ArtifactSbomReceipt(
                    artifact_id=artifact.artifact_id,
                    role=artifact.role,
                    platform=artifact.platform,
                    variant=artifact.variant,
                    oci_layout_digest=layout.root_digest,
                    image_digest=image.digest,
                    sbom_digest=_file_digest(path),
                    sbom_size_bytes=path.stat().st_size,
                    sbom_format=payload["bomFormat"],
                    sbom_spec_version=payload["specVersion"],
                )
            )
        except (OSError, ReleaseBundleError) as exc:
            raise ReleaseBundleCreateError("release_bundle_artifact_sbom_receipts_invalid") from exc
    try:
        return ArtifactSbomReceiptSet(receipts=tuple(receipts))
    except ReleaseBundleError as exc:
        raise ReleaseBundleCreateError("release_bundle_artifact_sbom_receipts_invalid") from exc


def _load_artifact_sbom_receipts(path: Path) -> ArtifactSbomReceiptSet:
    """Parse only the canonical public receipt set which the workflow attests."""

    if path.is_symlink() or not path.is_file():
        raise ReleaseBundleCreateError("release_bundle_artifact_sbom_receipts_invalid")
    try:
        raw = path.read_bytes()
        receipts = ArtifactSbomReceiptSet.from_dict(json.loads(raw))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ReleaseBundleError) as exc:
        raise ReleaseBundleCreateError("release_bundle_artifact_sbom_receipts_invalid") from exc
    if raw != _canonical_json(receipts.canonical_payload()):
        raise ReleaseBundleCreateError("release_bundle_artifact_sbom_receipts_noncanonical")
    return receipts


def _source_catalog_path(repository_root: Path, raw_path: str) -> Path:
    """Resolve exactly one relative, source-tree catalog path without escape."""

    candidate = Path(raw_path)
    if (
        not raw_path
        or candidate.is_absolute()
        or "\\" in raw_path
        or candidate.suffix != ".json"
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ReleaseBundleCreateError("release_bundle_model_catalog_path_invalid")
    try:
        root = repository_root.resolve(strict=True)
        unresolved = root / candidate
        resolved = unresolved.resolve(strict=True)
    except OSError as exc:
        raise ReleaseBundleCreateError("release_bundle_model_catalog_missing") from exc
    if (
        root not in (resolved, *resolved.parents)
        or unresolved.is_symlink()
        or not resolved.is_file()
    ):
        raise ReleaseBundleCreateError("release_bundle_model_catalog_path_invalid")
    return resolved


def _load_supported_catalog(repository_root: Path, catalog_path: str) -> ModelCatalog:
    path = _source_catalog_path(repository_root, catalog_path)
    payload = _load_json(path, "release_bundle_model_catalog_invalid")
    try:
        catalog = ModelCatalog.from_dict(payload)
    except (ReleaseBundleError, TypeError) as exc:
        raise ReleaseBundleCreateError("release_bundle_model_catalog_invalid") from exc
    if catalog.profile_id != _SUPPORTED_PROFILE or catalog.runtime != _SUPPORTED_RUNTIME:
        raise ReleaseBundleCreateError("release_bundle_catalog_profile_matrix_unsupported")
    if catalog.capability_contracts != _REQUIRED_CAPABILITIES:
        raise ReleaseBundleCreateError("release_bundle_catalog_capability_matrix_unsupported")
    return catalog


def _run_git(repository_root: Path, *arguments: str) -> bytes:
    """Return one trusted Git query result without exposing command output."""

    try:
        result = subprocess.run(
            ("git", "-C", str(repository_root), *arguments),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ReleaseBundleCreateError("release_bundle_source_checkout_unverified") from exc
    if result.returncode != 0:
        raise ReleaseBundleCreateError("release_bundle_source_checkout_unverified")
    return result.stdout


def _git_worktree_path_is_clean(repository_root: Path, *arguments: str) -> bool:
    """Return whether Git reports a tracked path set as clean."""

    try:
        result = subprocess.run(
            ("git", "-C", str(repository_root), "diff", "--quiet", *arguments),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ReleaseBundleCreateError("release_bundle_source_checkout_unverified") from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise ReleaseBundleCreateError("release_bundle_source_checkout_unverified")


def _verify_catalog_source_binding(
    repository_root: Path, *, source_revision: str, catalog_path: str
) -> None:
    """Require the catalog bytes and checkout to match the claimed source revision."""

    if _SOURCE_REVISION.fullmatch(source_revision) is None:
        raise ReleaseBundleCreateError("release_bundle_source_checkout_unverified")
    try:
        repository_root = repository_root.resolve(strict=True)
    except OSError as exc:
        raise ReleaseBundleCreateError("release_bundle_source_checkout_unverified") from exc
    head = _run_git(repository_root, "rev-parse", "HEAD").decode("ascii", errors="ignore").strip()
    if head != source_revision:
        raise ReleaseBundleCreateError("release_bundle_source_checkout_mismatch")
    if not _git_worktree_path_is_clean(repository_root, "--"):
        raise ReleaseBundleCreateError("release_bundle_source_checkout_dirty")
    if not _git_worktree_path_is_clean(repository_root, "--cached", "--"):
        raise ReleaseBundleCreateError("release_bundle_source_checkout_dirty")
    catalog_file = _source_catalog_path(repository_root, catalog_path)
    tracked_catalog = _run_git(repository_root, "ls-files", "--error-unmatch", "--", catalog_path)
    if not tracked_catalog:
        raise ReleaseBundleCreateError("release_bundle_source_checkout_unverified")
    source_catalog = _run_git(repository_root, "show", f"{source_revision}:{catalog_path}")
    try:
        local_catalog = catalog_file.read_bytes()
    except OSError as exc:
        raise ReleaseBundleCreateError("release_bundle_model_catalog_missing") from exc
    if source_catalog != local_catalog:
        raise ReleaseBundleCreateError("release_bundle_model_catalog_revision_mismatch")


def _tar_member(archive: tarfile.TarFile, name: str, code: str) -> tarfile.TarInfo:
    """Resolve one regular archive member without following a tar link."""

    try:
        member = archive.getmember(name)
    except KeyError as exc:
        raise ReleaseBundleCreateError(code) from exc
    if not member.isfile() or member.issym() or member.islnk():
        raise ReleaseBundleCreateError(code)
    return member


def _tar_member_bytes(archive: tarfile.TarFile, name: str, code: str) -> bytes:
    """Read one small regular archive member without following a tar link."""

    member = _tar_member(archive, name, code)
    handle = archive.extractfile(member)
    if handle is None:
        raise ReleaseBundleCreateError(code)
    try:
        payload = handle.read()
    except OSError as exc:
        raise ReleaseBundleCreateError(code) from exc
    if len(payload) != member.size:
        raise ReleaseBundleCreateError(code)
    return payload


def _tar_member_json(archive: tarfile.TarFile, name: str, code: str) -> Mapping[str, object]:
    try:
        payload = json.loads(_tar_member_bytes(archive, name, code))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBundleCreateError(code) from exc
    return _require_mapping(payload, code)


def _descriptor_digest(descriptor: Mapping[str, object]) -> str:
    return _require_digest(descriptor.get("digest"), "release_bundle_oci_layout_invalid")


def _descriptor_size(descriptor: Mapping[str, object]) -> int:
    size = descriptor.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid")
    return size


def _verify_descriptor_blob(
    archive: tarfile.TarFile, descriptor: Mapping[str, object], *, collect: bool
) -> bytes | None:
    """Hash an OCI descriptor blob without trusting its archive path or size."""

    digest = _descriptor_digest(descriptor)
    expected_size = _descriptor_size(descriptor)
    member = _tar_member(
        archive,
        f"blobs/sha256/{digest.removeprefix('sha256:')}",
        "release_bundle_oci_layout_invalid",
    )
    if member.size != expected_size:
        raise ReleaseBundleCreateError("release_bundle_oci_layout_digest_mismatch")
    handle = archive.extractfile(member)
    if handle is None:
        raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid")
    hasher = hashlib.sha256()
    byte_count = 0
    chunks: list[bytes] | None = [] if collect else None
    try:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
            byte_count += len(chunk)
            if chunks is not None:
                chunks.append(chunk)
    except OSError as exc:
        raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid") from exc
    actual_digest = f"sha256:{hasher.hexdigest()}"
    if byte_count != expected_size or actual_digest != digest:
        raise ReleaseBundleCreateError("release_bundle_oci_layout_digest_mismatch")
    if chunks is None:
        return None
    return b"".join(chunks)


def _blob_json(archive: tarfile.TarFile, descriptor: Mapping[str, object]) -> Mapping[str, object]:
    """Verify an OCI JSON descriptor against exact blob bytes before decoding it."""

    payload = _verify_descriptor_blob(archive, descriptor, collect=True)
    if payload is None:  # Defensive: collect=True always returns the payload.
        raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid") from exc
    return _require_mapping(decoded, "release_bundle_oci_layout_invalid")


def _descriptor_platform(descriptor: Mapping[str, object]) -> str | None:
    platform = descriptor.get("platform")
    if platform is None:
        return None
    mapping = _require_mapping(platform, "release_bundle_oci_layout_invalid")
    os_name = mapping.get("os")
    architecture = mapping.get("architecture")
    if not isinstance(os_name, str) or not isinstance(architecture, str):
        raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid")
    return f"{os_name}/{architecture}"


def _config_platform(config: Mapping[str, object]) -> str:
    os_name = config.get("os")
    architecture = config.get("architecture")
    if not isinstance(os_name, str) or not isinstance(architecture, str):
        raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid")
    return f"{os_name}/{architecture}"


def _config_labels(config: Mapping[str, object]) -> Mapping[str, str]:
    config_section = _require_mapping(config.get("config"), "release_bundle_oci_layout_invalid")
    labels = config_section.get("Labels", {})
    labels_mapping = _require_mapping(labels, "release_bundle_oci_layout_invalid")
    if not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels_mapping.items()
    ):
        raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid")
    return {str(key): str(value) for key, value in labels_mapping.items()}


def _read_oci_layout(path: Path) -> OciLayout:
    """Read one regular OCI tar and expose only digests, platforms, and labels."""

    if path.is_symlink() or not path.is_file():
        raise ReleaseBundleCreateError("release_bundle_oci_layout_missing")
    try:
        with tarfile.open(path, mode="r:*") as archive:
            index_payload = _tar_member_bytes(
                archive, "index.json", "release_bundle_oci_layout_invalid"
            )
            try:
                index = _require_mapping(
                    json.loads(index_payload), "release_bundle_oci_layout_invalid"
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid") from exc
            descriptors = _require_sequence(
                index.get("manifests"), "release_bundle_oci_layout_invalid"
            )
            if not descriptors:
                raise ReleaseBundleCreateError("release_bundle_oci_layout_root_invalid")
            # The OCI layout root is the exact index document.  A multi-platform
            # layout has one descriptor per platform here; it is not a malformed
            # single-image layout.  Hashing the raw bytes gives the immutable
            # manifest-list identity that a registry receives for this archive.
            root_digest = f"sha256:{hashlib.sha256(index_payload).hexdigest()}"
            images: dict[str, OciImage] = {}

            def visit(descriptor: Mapping[str, object], platform_hint: str | None = None) -> None:
                annotations = descriptor.get("annotations")
                if isinstance(annotations, Mapping) and (
                    annotations.get("vnd.docker.reference.type") == "attestation-manifest"
                ):
                    _verify_descriptor_blob(archive, descriptor, collect=False)
                    return
                digest = _descriptor_digest(descriptor)
                payload = _blob_json(archive, descriptor)
                nested = payload.get("manifests")
                if nested is not None:
                    nested_descriptors = _require_sequence(
                        nested, "release_bundle_oci_layout_invalid"
                    )
                    if not nested_descriptors:
                        raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid")
                    for child in nested_descriptors:
                        child_descriptor = _require_mapping(
                            child, "release_bundle_oci_layout_invalid"
                        )
                        visit(
                            child_descriptor,
                            _descriptor_platform(child_descriptor) or platform_hint,
                        )
                    return

                config_descriptor = _require_mapping(
                    payload.get("config"), "release_bundle_oci_layout_invalid"
                )
                layers = _require_sequence(
                    payload.get("layers"), "release_bundle_oci_layout_invalid"
                )
                for layer in layers:
                    _verify_descriptor_blob(
                        archive,
                        _require_mapping(layer, "release_bundle_oci_layout_invalid"),
                        collect=False,
                    )
                config = _blob_json(archive, config_descriptor)
                platform = (
                    _descriptor_platform(descriptor) or platform_hint or _config_platform(config)
                )
                if platform not in _PLATFORMS or platform in images:
                    raise ReleaseBundleCreateError("release_bundle_oci_layout_platform_invalid")
                images[platform] = OciImage(digest=digest, labels=_config_labels(config))

            for root in descriptors:
                root_descriptor = _require_mapping(root, "release_bundle_oci_layout_invalid")
                visit(root_descriptor, _descriptor_platform(root_descriptor))
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid") from exc
    if not images:
        raise ReleaseBundleCreateError("release_bundle_oci_layout_invalid")
    return OciLayout(root_digest=root_digest, images_by_platform=images)


def _validate_release_manifest(
    path: Path,
    *,
    release_version: str,
    source_revision: str,
    package_sbom_path: Path,
    edge_layout: OciLayout,
    server_layout: OciLayout,
    cpu_layout: OciLayout,
    cuda_layout: OciLayout,
) -> tuple[Mapping[str, object], str]:
    payload = _load_json(path, "release_bundle_release_manifest_invalid")
    mapping = _require_mapping(payload, "release_bundle_release_manifest_invalid")
    try:
        validate_release_manifest(mapping)
    except (ReleaseManifestError, ValueError) as exc:
        raise ReleaseBundleCreateError("release_bundle_release_manifest_invalid") from exc
    release = _require_mapping(mapping.get("release"), "release_bundle_release_manifest_invalid")
    source = _require_mapping(mapping.get("source"), "release_bundle_release_manifest_invalid")
    if release.get("semver") != release_version or source.get("commit") != source_revision:
        raise ReleaseBundleCreateError("release_bundle_release_manifest_identity_mismatch")
    if package_sbom_path.is_symlink() or not package_sbom_path.is_file():
        raise ReleaseBundleCreateError("release_bundle_package_sbom_invalid")
    sbom = _require_mapping(mapping.get("sbom"), "release_bundle_release_manifest_invalid")
    package_sbom_digest = _file_digest(package_sbom_path).removeprefix("sha256:")
    if (
        sbom.get("filename") != package_sbom_path.name
        or sbom.get("sha256") != package_sbom_digest
        or sbom.get("size_bytes") != package_sbom_path.stat().st_size
    ):
        raise ReleaseBundleCreateError("release_bundle_package_sbom_mismatch")
    images = _require_sequence(mapping.get("images"), "release_bundle_release_manifest_invalid")
    images_by_name: dict[str, Mapping[str, object]] = {}
    for image in images:
        image_mapping = _require_mapping(image, "release_bundle_release_manifest_invalid")
        name = image_mapping.get("name")
        if not isinstance(name, str) or name in images_by_name:
            raise ReleaseBundleCreateError("release_bundle_release_manifest_invalid")
        images_by_name[name] = image_mapping
    expected = {
        "local-edge": edge_layout.root_digest,
        "server": server_layout.root_digest,
        "inference-cpu": cpu_layout.root_digest,
        "inference-node": cuda_layout.root_digest,
    }
    if set(images_by_name) != set(expected):
        raise ReleaseBundleCreateError("release_bundle_release_manifest_image_mismatch")
    for name, digest in expected.items():
        image = images_by_name[name]
        reference = image.get("reference")
        if (
            image.get("digest") != digest
            or not isinstance(reference, str)
            or not reference.endswith(f"@{digest}")
        ):
            raise ReleaseBundleCreateError("release_bundle_release_manifest_image_mismatch")
    return mapping, _file_digest(path)


def _load_verified_evidence(path: Path) -> tuple[VerifiedEvidenceProjection, ...]:
    """Accept only the bounded evidence projection made after external verification."""

    payload = _load_json(path, "release_bundle_verified_evidence_invalid")
    items = _require_sequence(payload, "release_bundle_verified_evidence_invalid")
    try:
        return tuple(VerifiedEvidenceProjection.from_dict(item) for item in items)
    except ReleaseBundleError as exc:
        raise ReleaseBundleCreateError("release_bundle_verified_evidence_invalid") from exc


class _OciLayoutExecutor:
    """Adapter that turns already-built OCI layout evidence into materializations."""

    def __init__(
        self,
        *,
        layouts: Mapping[tuple[str, str], OciLayout],
        evidence_receipts: Mapping[str, ArtifactEvidenceReceipt],
    ) -> None:
        self._layouts = layouts
        self._evidence_receipts = evidence_receipts

    def materialize(self, plan: Any, artifact: Any) -> ArtifactMaterialization:
        layout = self._layouts.get((artifact.role, artifact.variant))
        if layout is None:
            raise ReleaseBundleCreateError("release_bundle_oci_matrix_incomplete")
        image = layout.images_by_platform.get(artifact.platform)
        if image is None:
            raise ReleaseBundleCreateError("release_bundle_oci_matrix_incomplete")
        expected_labels = plan.expected_oci_labels(artifact.artifact_id)
        if any(image.labels.get(key) != value for key, value in expected_labels.items()):
            raise ReleaseBundleCreateError("release_bundle_oci_labels_mismatch")
        evidence = self._evidence_receipts.get(artifact.artifact_id)
        if evidence is None:
            raise ReleaseBundleCreateError("release_bundle_artifact_evidence_matrix_incomplete")
        if (
            evidence.oci_layout_digest != layout.root_digest
            or evidence.image_digest != image.digest
            or evidence.oci_labels_digest != _digest_payload(expected_labels)
        ):
            raise ReleaseBundleCreateError("release_bundle_artifact_evidence_mismatch")
        return ArtifactMaterialization(
            artifact_id=artifact.artifact_id,
            role=artifact.role,
            platform=artifact.platform,
            variant=artifact.variant,
            immutable_reference=f"oci@{image.digest}",
            image_digest=image.digest,
            oci_layout_digest=evidence.oci_layout_digest,
            oci_labels_digest=_digest_payload(expected_labels),
            sbom_digest=evidence.sbom_digest,
            provenance_digest=evidence.provenance_digest,
            evidence_receipt=evidence,
        )


def _load_artifact_evidence_receipts(
    paths: Sequence[str], *, plan: Any
) -> dict[str, ArtifactEvidenceReceipt]:
    """Load one exact verifier receipt for every expanded artifact in the plan."""

    receipts: dict[str, ArtifactEvidenceReceipt] = {}
    descriptors = {artifact.artifact_id: artifact for artifact in plan.artifacts}
    for raw_path in paths:
        payload = _load_json(Path(raw_path), "release_bundle_artifact_evidence_invalid")
        try:
            receipt = ArtifactEvidenceReceipt.from_dict(payload)
        except ContainerArtifactError as exc:
            raise ReleaseBundleCreateError("release_bundle_artifact_evidence_invalid") from exc
        descriptor = descriptors.get(receipt.artifact_id)
        if descriptor is None or receipt.artifact_id in receipts:
            raise ReleaseBundleCreateError("release_bundle_artifact_evidence_matrix_invalid")
        try:
            receipt.validate_against(plan, descriptor)
        except ContainerArtifactError as exc:
            raise ReleaseBundleCreateError("release_bundle_artifact_evidence_mismatch") from exc
        receipts[receipt.artifact_id] = receipt
    if set(receipts) != set(descriptors):
        raise ReleaseBundleCreateError("release_bundle_artifact_evidence_matrix_incomplete")
    return receipts


def _write_bytes(path: Path, payload: bytes) -> None:
    """Write one deterministic evidence file without replacing attested bytes."""

    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ReleaseBundleCreateError("release_bundle_output_invalid")
        try:
            if path.read_bytes() == payload:
                return
        except OSError as exc:
            raise ReleaseBundleCreateError("release_bundle_output_invalid") from exc
        raise ReleaseBundleCreateError("release_bundle_output_exists")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    except OSError as exc:
        raise ReleaseBundleCreateError("release_bundle_output_invalid") from exc


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )


def _create_bundle(args: argparse.Namespace) -> dict[str, Path]:
    repository_root = Path(args.repository_root).resolve()
    if not repository_root.is_dir() or _SOURCE_REVISION.fullmatch(args.source_revision) is None:
        raise ReleaseBundleCreateError("release_bundle_input_invalid")
    try:
        package_version_for_release(args.release_version)
    except ReleaseManifestError as exc:
        raise ReleaseBundleCreateError("release_bundle_input_invalid") from exc

    _verify_catalog_source_binding(
        repository_root,
        source_revision=args.source_revision,
        catalog_path=args.model_catalog_path,
    )
    catalog = _load_supported_catalog(repository_root, args.model_catalog_path)
    server_layout = _read_oci_layout(Path(args.server_oci_layout))
    edge_layout = _read_oci_layout(Path(args.edge_oci_layout))
    cpu_layout = _read_oci_layout(Path(args.compute_cpu_oci_layout))
    cuda_layout = _read_oci_layout(Path(args.compute_cuda_oci_layout))
    release_manifest_path = Path(args.release_manifest)
    _, manifest_digest = _validate_release_manifest(
        release_manifest_path,
        release_version=args.release_version,
        source_revision=args.source_revision,
        package_sbom_path=Path(args.sbom),
        edge_layout=edge_layout,
        server_layout=server_layout,
        cpu_layout=cpu_layout,
        cuda_layout=cuda_layout,
    )
    compiler = ContainerArtifactCompiler()
    plan = compiler.prepare(
        ArtifactRequest(
            profile_id=_SUPPORTED_PROFILE,
            source_revision=args.source_revision,
            package_version=package_version_for_release(args.release_version),
            platforms=_PLATFORMS,
            compute_variants=(COMPUTE_VARIANT_CPU, COMPUTE_VARIANT_CUDA),
            model_catalog_reference=catalog.catalog_id,
            model_catalog_digest=catalog.digest,
        )
    )
    layouts = {
        (PP_LOCAL_EDGE, "standard"): edge_layout,
        (PP_SERVER_BACKEND, "standard"): server_layout,
        (PP_COMPUTE_NODE, COMPUTE_VARIANT_CPU): cpu_layout,
        (PP_COMPUTE_NODE, COMPUTE_VARIANT_CUDA): cuda_layout,
    }
    derived_artifact_sbom_receipts = _derive_artifact_sbom_receipts(
        args.artifact_sbom,
        plan=plan,
        layouts=layouts,
    )
    artifact_evidence_receipts = _load_artifact_evidence_receipts(
        args.artifact_evidence,
        plan=plan,
    )
    bundle = compiler.materialize(
        plan,
        _OciLayoutExecutor(
            layouts=layouts,
            evidence_receipts=artifact_evidence_receipts,
        ),
    )
    artifact_sbom_receipts_path = Path(args.artifact_sbom_receipts)
    expected_receipts_bytes = _canonical_json(derived_artifact_sbom_receipts.canonical_payload())
    _write_bytes(artifact_sbom_receipts_path, expected_receipts_bytes)
    recorded_artifact_sbom_receipts = _load_artifact_sbom_receipts(artifact_sbom_receipts_path)
    if recorded_artifact_sbom_receipts.digest != derived_artifact_sbom_receipts.digest:
        raise ReleaseBundleCreateError("release_bundle_artifact_sbom_receipts_mismatch")
    try:
        binding = ArtifactBundleBinding.from_artifact_bundle(
            bundle,
            artifact_sbom_receipts=recorded_artifact_sbom_receipts,
        )
    except ReleaseBundleError as exc:
        raise ReleaseBundleCreateError("release_bundle_artifact_binding_invalid") from exc

    output_dir = Path(args.output_dir)
    model_catalog_path = output_dir / "model-catalog.json"
    artifact_binding_path = output_dir / "artifact-binding.json"
    release_bundle_path = output_dir / "release-bundle.json"
    # These two source artifacts must hash to the semantic digest consumed by
    # ``ReleaseBundle``.  Do not serialize display-only digest fields or pretty
    # whitespace here: GitHub attests the file bytes, while the typed contract
    # validates the same canonical payload digest.
    _write_bytes(model_catalog_path, canonical_model_catalog_bytes(catalog))
    # The receipt file remains independently attestable.  The binding carries
    # its canonical digest, allowing a release verifier to require both files.
    _write_bytes(artifact_binding_path, _canonical_json(binding.canonical_payload()))
    try:
        recorded_binding = ArtifactBundleBinding.from_dict(
            _load_json(artifact_binding_path, "release_bundle_artifact_binding_invalid")
        )
        recorded_binding.validate_artifact_sbom_receipts(recorded_artifact_sbom_receipts)
    except ReleaseBundleError as exc:
        raise ReleaseBundleCreateError("release_bundle_artifact_binding_invalid") from exc
    if recorded_binding.digest != binding.digest:
        raise ReleaseBundleCreateError("release_bundle_artifact_binding_invalid")
    binding = recorded_binding

    outputs: dict[str, Path] = {
        "model_catalog": model_catalog_path,
        "artifact_sbom_receipts": artifact_sbom_receipts_path,
        "artifact_binding": artifact_binding_path,
    }
    if args.prepare_only:
        return outputs

    try:
        evidence = _load_verified_evidence(Path(args.verified_evidence))
        release_bundle = ReleaseBundle(
            release_version=args.release_version,
            source_revision=args.source_revision,
            release_manifest_sha256=manifest_digest.removeprefix("sha256:"),
            artifact_binding=binding,
            model_catalog=catalog,
            attestation_evidence=evidence,
        )
    except ReleaseBundleError as exc:
        raise ReleaseBundleCreateError("release_bundle_evidence_invalid") from exc
    _write_json(release_bundle_path, release_bundle.to_dict())
    outputs["release_bundle"] = release_bundle_path
    return outputs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="Source checkout containing the tracked model catalog",
    )
    parser.add_argument(
        "--model-catalog-path",
        required=True,
        help="Strict relative path to a source-controlled model catalog JSON file",
    )
    parser.add_argument(
        "--validate-catalog-only",
        action="store_true",
        help="Validate only the source-controlled catalog before expensive builds",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Write catalog and strict artifact binding, but never create a release bundle",
    )
    parser.add_argument("--release-version", help="RC SemVer such as v1.2.3-rc.1")
    parser.add_argument("--source-revision", help="Immutable source revision")
    parser.add_argument("--release-manifest", type=Path, help="Exact generated release manifest")
    parser.add_argument("--sbom", type=Path, help="Exact generated package CycloneDX SBOM")
    parser.add_argument(
        "--artifact-sbom",
        action="append",
        default=[],
        metavar="ARTIFACT_ID=PATH",
        help="Exact per-artifact CycloneDX SBOM; repeat once for every OCI artifact",
    )
    parser.add_argument(
        "--artifact-evidence",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Exact plan-bound OCI SBOM/provenance verifier receipt; repeat once for every "
            "expanded artifact"
        ),
    )
    parser.add_argument(
        "--artifact-sbom-receipts",
        type=Path,
        help=(
            "Canonical public OCI-to-SBOM receipt set; created in prepare mode and "
            "revalidated before the final bundle"
        ),
    )
    parser.add_argument("--edge-oci-layout", type=Path, help="Local-edge OCI layout tar")
    parser.add_argument("--server-oci-layout", type=Path, help="Server OCI layout tar")
    parser.add_argument("--compute-cpu-oci-layout", type=Path, help="CPU compute OCI layout tar")
    parser.add_argument("--compute-cuda-oci-layout", type=Path, help="CUDA compute OCI layout tar")
    parser.add_argument(
        "--verified-evidence",
        type=Path,
        help="Externally verified evidence projection for the four pre-bundle subjects",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("dist"), help="Generated evidence dir"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.validate_catalog_only:
            if args.source_revision is not None:
                _verify_catalog_source_binding(
                    Path(args.repository_root).resolve(),
                    source_revision=args.source_revision,
                    catalog_path=args.model_catalog_path,
                )
            _load_supported_catalog(Path(args.repository_root).resolve(), args.model_catalog_path)
            print("release_bundle_model_catalog_valid")
            return 0
        required_arguments = (
            args.release_version,
            args.source_revision,
            args.release_manifest,
            args.sbom,
            args.edge_oci_layout,
            args.server_oci_layout,
            args.compute_cpu_oci_layout,
            args.compute_cuda_oci_layout,
            args.artifact_sbom_receipts,
        )
        if any(value is None for value in required_arguments):
            raise ReleaseBundleCreateError("release_bundle_arguments_incomplete")
        if not args.artifact_sbom:
            raise ReleaseBundleCreateError("release_bundle_artifact_sbom_matrix_incomplete")
        if not args.artifact_evidence:
            raise ReleaseBundleCreateError("release_bundle_artifact_evidence_matrix_incomplete")
        if not args.prepare_only and args.verified_evidence is None:
            raise ReleaseBundleCreateError("release_bundle_verified_evidence_required")
        outputs = _create_bundle(args)
    except ReleaseBundleCreateError as exc:
        print(f"release_bundle_create_failed:{exc.code}", file=sys.stderr)
        return 2
    print(
        "release_bundle_created="
        + json.dumps({key: str(value) for key, value in outputs.items()}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
